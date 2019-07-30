import os
import signal
from abc import ABCMeta, abstractmethod
from argparse import Namespace
from collections import defaultdict, deque
from typing import List, Optional, Tuple, Dict

from bxcommon import constants
from bxcommon.connections.abstract_connection import AbstractConnection
from bxcommon.connections.connection_pool import ConnectionPool
from bxcommon.connections.connection_state import ConnectionState
from bxcommon.connections.connection_type import ConnectionType
from bxcommon.exceptions import TerminationError
from bxcommon.messages.abstract_message import AbstractMessage
from bxcommon.network.socket_connection import SocketConnection
from bxcommon.services import sdn_http_service
from bxcommon.utils import logger, memory_utils, json_utils
from bxcommon.utils.alarm_queue import AlarmQueue
from bxcommon.utils.stats.block_statistics_service import block_stats
from bxcommon.utils.stats.memory_statistics_service import memory_statistics
from bxcommon.utils.stats.node_info_service import node_info_statistics
from bxcommon.utils.stats.throughput_service import throughput_statistics
from bxcommon.utils.stats.transaction_statistics_service import tx_stats


class AbstractNode:
    __meta__ = ABCMeta
    FLUSH_SEND_BUFFERS_INTERVAL = constants.OUTPUT_BUFFER_BATCH_MAX_HOLD_TIME * 2
    NODE_TYPE = None

    def __init__(self, opts: Namespace):
        logger.info("Initializing node of type: {}", self.NODE_TYPE)

        self.set_node_config_opts_from_sdn(opts)
        self.opts = opts
        self.connection_queue = deque()
        self.disconnect_queue = deque()
        self.outbound_peers = opts.outbound_peers[:]

        self.connection_pool = ConnectionPool()

        self.schedule_pings_on_timeout = False
        self.should_force_exit = False

        self.num_retries_by_ip: Dict[Tuple[str, int], int] = defaultdict(int)

        # Handle termination gracefully
        signal.signal(signal.SIGTERM, self._kill_node)
        signal.signal(signal.SIGINT, self._kill_node)
        signal.signal(signal.SIGSEGV, self._kill_node)

        # Event handling queue for delayed events
        self.alarm_queue = AlarmQueue()

        self.tx_service = None

        self.init_throughput_logging()
        self.init_node_info_logging()
        self.init_memory_stats_logging()
        self.init_block_stats_logging()
        self.init_tx_stats_logging()

        # TODO: clean this up alongside outputbuffer holding time
        # this is Nagle's algorithm and we need to implement it properly
        # flush buffers regularly because of output buffer holding time
        self.alarm_queue.register_approx_alarm(self.FLUSH_SEND_BUFFERS_INTERVAL,
                                               constants.OUTPUT_BUFFER_BATCH_MAX_HOLD_TIME,
                                               self.flush_all_send_buffers)

        self.network_num = opts.blockchain_network_num

        # converting setting in MB to bytes
        self.next_report_mem_usage_bytes = self.opts.dump_detailed_report_at_memory_usage * 1024 * 1024

        if opts.dump_removed_short_ids:
            os.makedirs(opts.dump_removed_short_ids_path, exist_ok=True)

    def get_sdn_address(self):
        """
        Placeholder for net event loop to get the sdn address (relay only).
        :return:
        """
        return

    @abstractmethod
    def get_tx_service(self, network_num=None):
        pass

    @abstractmethod
    def get_outbound_peer_addresses(self):
        pass

    def connection_exists(self, ip, port):
        return self.connection_pool.has_connection(ip, port)

    # TODO: This needs a better name... "notify_connection"
    # TODO: we cannot call this a "socket_connection" It is very unclear what the term "connection" means in our
    # codebase.
    def on_connection_added(self, socket_connection, ip, port, from_me):
        """
        Notifies the node that a connection is coming in.
        """

        if not isinstance(socket_connection, SocketConnection):
            raise ValueError("Type SocketConnection is expected for socket_connection argument but was {0}"
                             .format(socket_connection))

        fileno = socket_connection.fileno()

        # If we're already connected to the remote peer, log the event and request disconnect.
        if self.connection_exists(ip, port):
            logger.warn("Duplicate connection attempted to: {0}:{1}.", ip, port)

            # Schedule dropping the added connection and keep the old one.
            self.enqueue_disconnect(fileno)
        else:
            self._initialize_connection(socket_connection, ip, port, from_me)

    def on_connection_initialized(self, fileno):
        conn = self.connection_pool.get_by_fileno(fileno)

        if conn is None:
            logger.warn("Initialized connection not in pool. Fileno: {0}", fileno)
            return

        logger.info("Connection state initialized: {}", conn)
        conn.state |= ConnectionState.INITIALIZED

    def on_connection_closed(self, fileno):
        conn = self.connection_pool.get_by_fileno(fileno)

        if conn is None:
            logger.warn("Connection not in pool. Fileno: {0}", fileno)
            return

        logger.info("Destroying connection: {}", conn)
        # Only retry for connections that are from me
        self.destroy_conn(conn, retry_connection=conn.from_me)

    @abstractmethod
    def send_request_for_relay_peers(self):
        pass

    def on_updated_peers(self, outbound_peer_models):
        if not outbound_peer_models:
            logger.warn("Got peer update with no peers.")
            return

        logger.trace("Processing updated outbound peers: {}.", outbound_peer_models)

        # Remove peers not in updated list or from command-line args.
        remove_peers = []
        old_peers = self.outbound_peers
        for old_peer in old_peers:
            if not (any(old_peer.ip == fixed_peer.ip and old_peer.port == fixed_peer.port
                        for fixed_peer in self.opts.outbound_peers)
                    or any(new_peer.ip == old_peer.ip and new_peer.port == old_peer.port
                           for new_peer in outbound_peer_models)):
                remove_peers.append(old_peer)

        for rem_peer in remove_peers:
            if self.connection_pool.has_connection(rem_peer.ip, rem_peer.port):
                rem_conn = self.connection_pool.get_by_ipport(rem_peer.ip,
                                                              rem_peer.port)
                if rem_conn:
                    self.destroy_conn(rem_conn)

        # Connect to peers not in our known pool
        for peer in outbound_peer_models:
            peer_ip = peer.ip
            peer_port = peer.port
            if not self.connection_pool.has_connection(peer_ip, peer_port):
                self.enqueue_connection(peer_ip, peer_port)
        self.outbound_peers = outbound_peer_models

    def on_updated_sid_space(self, sid_start, sid_end):
        """
        Placeholder interface to receive sid updates from SDN over sockets and pass to relay node
        """

        return

    def on_bytes_received(self, fileno: int, bytes_received: bytearray) -> None:
        """
        :param fileno:
        :param bytes_received:
        :return: True if the node should continue receiving bytes from the remote peer. False otherwise.
        """
        conn = self.connection_pool.get_by_fileno(fileno)

        if conn is None:
            logger.warn("Received bytes for connection not in pool. Fileno {0}", fileno)
            return

        if conn.state & ConnectionState.MARK_FOR_CLOSE:
            return

        conn.add_received_bytes(bytes_received)

        if conn.state & ConnectionState.MARK_FOR_CLOSE:
            self.destroy_conn(conn)

    def on_finished_receiving(self, fileno):
        conn = self.connection_pool.get_by_fileno(fileno)

        if conn is None:
            logger.warn("Received bytes for connection not in pool. Fileno {0}", fileno)
            return

        if conn.state & ConnectionState.MARK_FOR_CLOSE:
            return

        conn.process_message()

    def get_bytes_to_send(self, fileno):
        conn = self.connection_pool.get_by_fileno(fileno)

        if conn is None:
            logger.warn("Request to get bytes for connection not in pool. Fileno {0}", fileno)
            return

        if conn.state & ConnectionState.MARK_FOR_CLOSE:
            return

        return conn.get_bytes_to_send()

    def on_bytes_sent(self, fileno, bytes_sent):
        conn = self.connection_pool.get_by_fileno(fileno)

        if conn is None:
            logger.warn("Bytes sent call for connection not in pool. Fileno {0}", fileno)
            return

        conn.advance_sent_bytes(bytes_sent)

    def get_sleep_timeout(self, triggered_by_timeout, first_call=False):
        # TODO: remove first_call from this function. You can just fire all of the ready alarms on every call
        # to get the timeout.
        if first_call:
            _, timeout = self.alarm_queue.time_to_next_alarm()

            # Time out can be negative during debugging
            if timeout < 0:
                timeout = constants.DEFAULT_SLEEP_TIMEOUT

            return timeout
        else:
            time_to_next = self.alarm_queue.fire_ready_alarms(triggered_by_timeout)
            if self.connection_queue or self.disconnect_queue:
                # TODO: this should be constants.MIN_SLEEP_TIMEOUT, which is different for kqueues and epoll.
                # We want to process connection/disconnection requests ASAP.
                time_to_next = constants.DEFAULT_SLEEP_TIMEOUT

            return time_to_next

    def force_exit(self):
        """
        Indicates if node should trigger exit in event loop. Primarily used for testing.

        Typically requires one additional socket call (e.g. connecting to this node via a socket)
        to finish terminating the event loop.
        """
        return self.should_force_exit

    def close(self):
        logger.error("Node is closing! Closing everything.")

        self.cleanup_memory_stats_logging()

        for _fileno, conn in self.connection_pool.items():
            self.destroy_conn(conn)

    def broadcast(self, msg: AbstractMessage, broadcasting_conn: Optional[AbstractConnection] = None,
                  prepend_to_queue: bool = False, network_num: Optional[int] = None,
                  connection_types: Optional[List[ConnectionType]] = None, exclude_relays: bool = False) \
            -> List[AbstractConnection]:
        """
        Broadcasts message msg to connections of the specified type except requester.
        """
        if connection_types is None:
            connection_types = [ConnectionType.RELAY_ALL]

        if broadcasting_conn is not None:
            logger.log(msg.log_level(), "Broadcasting {} to [{}] connections from {}.",
                       msg, ",".join(map(str, connection_types)), broadcasting_conn)
        else:
            logger.log(msg.log_level(), "Broadcasting {} to [{}] connections.", msg,
                       ",".join(map(str, connection_types)))

        if network_num is None:
            broadcast_net_num = self.network_num
        else:
            broadcast_net_num = network_num

        connections_by_types = set()
        for connection_type in connection_types:
            connections_by_types.update(self.connection_pool.get_by_connection_type(connection_type))

        broadcast_connections = []
        for conn in connections_by_types:
            is_matching_network_num = (not exclude_relays and conn.network_num == constants.ALL_NETWORK_NUM) or \
                                      conn.network_num == broadcast_net_num
            if conn.is_active() and conn != broadcasting_conn and is_matching_network_num:
                conn.enqueue_msg(msg, prepend_to_queue)
                broadcast_connections.append(conn)

        return broadcast_connections

    @abstractmethod
    def build_connection(self, socket_connection: SocketConnection, ip: str, port: int, from_me: bool = False) \
            -> Optional[AbstractConnection]:
        pass

    def enqueue_connection(self, ip, port):
        """
        Add address to the queue of outbound connections
        """
        logger.debug("Enqueuing connection to {}:{}", ip, port)
        self.connection_queue.append((ip, port))

    def enqueue_disconnect(self, fileno):
        """
        Add address to the queue of connections to disconnect
        """
        logger.debug("Enqueuing disconnect from {}", fileno)
        self.disconnect_queue.append(fileno)

    def pop_next_connection_address(self):
        """
        Get next address from the queue of outbound connections

        :return: tuple (ip, port)
        """

        if self.connection_queue:
            return self.connection_queue.popleft()

        return

    def pop_next_disconnect_connection(self):
        """
        Get next Fileno from the queue of disconnect connections

        :return: int (fileno)
        """

        if self.disconnect_queue:
            return self.disconnect_queue.popleft()

        return

    def destroy_conn(self, conn, retry_connection=False):
        """
        Clean up the associated connection and update all data structures tracking it.
        We also retry trusted connections since they can never be destroyed.
        """

        logger.info("Breaking connection to {}. Attempting retry: {}", conn, retry_connection)

        self.connection_pool.delete(conn)
        conn.mark_for_close()

        peer_ip, peer_port = conn.peer_ip, conn.peer_port
        if retry_connection:
            if self.is_outbound_peer(peer_ip, peer_port) or \
                    conn.CONNECTION_TYPE == ConnectionType.BLOCKCHAIN_NODE or \
                    conn.CONNECTION_TYPE == ConnectionType.REMOTE_BLOCKCHAIN_NODE or \
                    conn.CONNECTION_TYPE == ConnectionType.SDN:
                self.alarm_queue.register_alarm(constants.CONNECTION_RETRY_SECONDS, self._retry_init_client_socket,
                                                peer_ip, peer_port, conn.CONNECTION_TYPE)
        else:
            self.on_failed_connection_retry(peer_ip, peer_port, conn.CONNECTION_TYPE)

        self.enqueue_disconnect(conn.fileno)

    def is_outbound_peer(self, ip, port):
        return any(peer.ip == ip and peer.port == port for peer in self.outbound_peers)

    def should_retry_connection(self, ip: str, port: int, connection_type: ConnectionType) -> bool:
        is_sdn = bool(connection_type & ConnectionType.SDN)
        return is_sdn or self.num_retries_by_ip[(ip, port)] < constants.MAX_CONNECT_RETRIES

    def on_failed_connection_retry(self, ip: str, port: int, connection_type: ConnectionType) -> None:
        if connection_type & ConnectionType.RELAY_ALL:
            sdn_http_service.submit_peer_connection_error_event(self.opts.node_id, ip, port)
            self.send_request_for_relay_peers()

    def init_throughput_logging(self):
        throughput_statistics.set_node(self)
        self.alarm_queue.register_alarm(constants.THROUGHPUT_STATS_INTERVAL, throughput_statistics.flush_info)

    def init_node_info_logging(self):
        node_info_statistics.set_node(self)
        self.alarm_queue.register_alarm(constants.INFO_STATS_INTERVAL, node_info_statistics.flush_info)

    def init_memory_stats_logging(self):
        memory_statistics.set_node(self)
        memory_statistics.start_recording(self.record_mem_stats)

    def cleanup_memory_stats_logging(self):
        memory_statistics.stop_recording()

    def init_block_stats_logging(self):
        block_stats.set_node(self)

    def init_tx_stats_logging(self):
        tx_stats.set_node(self)

    def flush_all_send_buffers(self):
        for conn in self.connection_pool:
            if conn.socket_connection.can_send:
                conn.socket_connection.send()
        return self.FLUSH_SEND_BUFFERS_INTERVAL

    def record_mem_stats(self):
        """
        When overridden, records identified memory stats and flushes them to std out
        :returns memory stats flush interval
        """

        self.connection_pool.log_connection_pool_mem_stats()
        return memory_statistics.flush_info()

    def set_node_config_opts_from_sdn(self, opts):

        # TODO: currently hard-coding configuration values
        opts.stats_calculate_actual_size = False
        opts.log_detailed_block_stats = False

    def dump_memory_usage(self):
        total_mem_usage = memory_utils.get_app_memory_usage()

        if total_mem_usage >= self.next_report_mem_usage_bytes:
            node_size = memory_utils.get_detailed_object_size(self)
            logger.statistics(
                "Application consumed {} bytes which is over set limit {} bytes. Detailed memory report: {}",
                total_mem_usage, self.next_report_mem_usage_bytes, json_utils.serialize(node_size))
            self.next_report_mem_usage_bytes = total_mem_usage + constants.MEMORY_USAGE_INCREASE_FOR_NEXT_REPORT_BYTES

    def on_input_received(self, file_no: int) -> bool:
        """handles an input event from the event loop

        :param file_no: the socket connection file_no
        :return: True if the connection is receivable, otherwise False
        """
        connection = self.connection_pool.get_by_fileno(file_no)
        if connection is None:
            return False
        return connection.on_input_received()

    def _initialize_connection(self, socket_connection: SocketConnection, ip: str, port: int, from_me: bool):
        conn_obj = self.build_connection(socket_connection, ip, port, from_me)
        if conn_obj is not None:
            logger.info("Adding connection: {}.", conn_obj)

            self.alarm_queue.register_alarm(constants.CONNECTION_TIMEOUT, self._connection_timeout, conn_obj)
            self.connection_pool.add(socket_connection.fileno(), ip, port, conn_obj)

            if conn_obj.CONNECTION_TYPE == ConnectionType.SDN:
                self.sdn_connection = conn_obj
        else:
            self.enqueue_disconnect(socket_connection.fileno())

    def _connection_timeout(self, conn):
        """
        Check if the connection is established.
        If it is not established, we give up for untrusted connections and try again for trusted connections.
        """

        logger.debug("Checking connection status: {}", conn)

        if conn.state & ConnectionState.ESTABLISHED:
            logger.debug("Connection is still established: {}", conn)

            if self.schedule_pings_on_timeout:
                self.alarm_queue.register_alarm(constants.PING_INTERVAL_S, conn.send_ping)

            return constants.CANCEL_ALARMS

        if conn.state & ConnectionState.MARK_FOR_CLOSE:
            logger.debug("Connection has already been marked for closure: {}", conn)
            return constants.CANCEL_ALARMS

        # Clean up the old connection and retry it if it is trusted
        logger.debug("Connection has timed out: {}", conn)
        self.destroy_conn(conn, retry_connection=True)

        # It is connect_to_address's job to schedule this function.
        return constants.CANCEL_ALARMS

    def _kill_node(self, _signum, _stack):
        """
        Kills the node immediately
        """
        logger.fatal("Node has been killed")
        raise TerminationError("Node killed.")

    def _retry_init_client_socket(self, ip, port, connection_type):
        self.num_retries_by_ip[(ip, port)] += 1

        if self.should_retry_connection(ip, port, connection_type):
            logger.info("Retrying {} connection to {}:{}. Attempt #{}.", connection_type, ip, port,
                        self.num_retries_by_ip[(ip, port)])
            self.enqueue_connection(ip, port)
        else:
            del self.num_retries_by_ip[(ip, port)]
            logger.warn("Maximum retry attempts exceeded. Dropping {} connection to {}:{}.", connection_type, ip, port)
            self.on_failed_connection_retry(ip, port, connection_type)

        return 0
