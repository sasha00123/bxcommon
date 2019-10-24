from mock import patch, MagicMock

from bxcommon import constants
from bxcommon.connections.abstract_node import AbstractNode
from bxcommon.connections.connection_state import ConnectionState
from bxcommon.connections.connection_type import ConnectionType
from bxcommon.constants import DEFAULT_SLEEP_TIMEOUT, MAX_CONNECT_RETRIES, FIRST_STATS_INTERVAL_S
from bxcommon.exceptions import TerminationError
from bxcommon.models.outbound_peer_model import OutboundPeerModel
from bxcommon.test_utils import helpers
from bxcommon.test_utils.abstract_test_case import AbstractTestCase
from bxcommon.test_utils.mocks.mock_connection import MockConnection
from bxcommon.test_utils.mocks.mock_node import MockNode
from bxcommon.test_utils.mocks.mock_socket_connection import MockSocketConnection
from bxcommon.utils import memory_utils
from bxcommon.utils.stats.throughput_service import throughput_statistics


class AbstractNodeTest(AbstractTestCase):
    def setUp(self):
        self.remote_node = MockNode(helpers.get_common_opts(4321, external_ip="1.1.1.1"))
        self.remote_ip = "111.222.111.222"
        self.remote_port = 1234
        self.remote_fileno = 5
        self.connection = helpers.create_connection(MockConnection, self.remote_node, fileno=self.remote_fileno,
                                                    ip=self.remote_ip, port=self.remote_port)
        self.connection = MockConnection(MockSocketConnection(self.remote_fileno), (self.remote_ip, self.remote_port),
                                         self.remote_node)
        self.local_node = TestNode(helpers.get_common_opts(8888))
        self.socket_connection = MockSocketConnection(node=self.remote_node)
        self.data = bytearray([i for i in range(32)])

    def test_connection_exists(self):
        self.assertFalse(self.local_node.connection_exists(self.remote_ip, self.remote_port))
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.assertTrue(self.local_node.connection_exists(self.remote_ip, self.remote_port))

    @patch("bxcommon.connections.abstract_node.AbstractNode._initialize_connection")
    def test_on_connection_added_new_connection(self, mocked_initialize_connection):
        self.local_node.on_connection_added(self.socket_connection, self.remote_ip, self.remote_port, True)
        mocked_initialize_connection.assert_called_once_with(self.socket_connection, self.remote_ip, self.remote_port,
                                                             True)

    @patch("bxcommon.connections.abstract_node.AbstractNode.enqueue_disconnect")
    @patch("bxcommon.connections.abstract_node.AbstractNode.connection_exists", return_value=True)
    def test_on_connection_added_duplicate(self, mocked_enqueue_disconnect, mocked_connection_exists):
        self.local_node.on_connection_added(self.socket_connection, self.remote_ip, self.remote_port, True)
        mocked_enqueue_disconnect.assert_called_once_with(self.remote_ip, self.remote_port)

    def test_on_connection_initialized(self):
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.assertEqual(ConnectionState.CONNECTING, self.connection.state)
        self.local_node.on_connection_initialized(self.remote_fileno)
        self.assertEqual(ConnectionState.INITIALIZED, self.connection.state)

    def test_on_connection_closed(self):
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.assertIn(self.connection, self.local_node.connection_pool.by_fileno)
        self.local_node.on_connection_closed(self.remote_fileno)
        self.assertNotIn(self.connection, self.local_node.connection_pool.by_fileno)

    @patch("bxcommon.connections.abstract_node.AbstractNode.destroy_conn")
    def test_on_updated_peers(self, mocked_destroy_conn):
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.local_node.opts.outbound_peers = [OutboundPeerModel("222.222.222.222", 2000)]
        self.local_node.outbound_peers = [OutboundPeerModel("111.111.111.111", 1000),
                                          OutboundPeerModel("222.222.222.222", 2000),
                                          OutboundPeerModel(self.remote_ip, self.remote_port)]

        outbound_peer_models = [OutboundPeerModel("111.111.111.111", 1000)]
        self.local_node.on_updated_peers(outbound_peer_models)
        self.assertEqual(outbound_peer_models, self.local_node.outbound_peers)
        mocked_destroy_conn.assert_called_with(self.connection)

    def test_on_bytes_received(self):
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.local_node.on_bytes_received(self.remote_fileno, self.data)
        self.assertEqual(self.data, self.connection.inputbuf.input_list[0])

    def test_on_bytes_received_connection_marked_for_close(self):
        self.local_node.destroy_conn = MagicMock()
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.connection.mark_for_close()
        self.local_node.on_bytes_received(self.remote_fileno, self.data)
        self.local_node.destroy_conn.assert_not_called()

    def test_on_finished_receiving(self):
        self.connection.process_message = MagicMock()
        self.local_node.destroy_conn = MagicMock()

        some_other_fileno = 1
        self.local_node.on_finished_receiving(some_other_fileno)
        self.connection.process_message.assert_not_called()

        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.connection.state = ConnectionState.INITIALIZED

        self.local_node.on_finished_receiving(self.remote_fileno)
        self.connection.process_message.assert_called_once()
        self.local_node.destroy_conn.assert_not_called()
        self.connection.process_message.reset_mock()

        self.connection.process_message.side_effect = lambda: self.connection.mark_for_close()
        self.local_node.on_finished_receiving(self.remote_fileno)
        self.connection.process_message.assert_called_once()
        self.local_node.destroy_conn.assert_called_once()

    def test_get_bytes_to_send(self):
        self.connection.outputbuf.output_msgs.append(self.data)
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.assertEqual(self.data, self.local_node.get_bytes_to_send(self.remote_fileno))

    def test_on_bytes_sent(self):
        self.connection.outputbuf.output_msgs.append(self.data)
        self.connection.outputbuf.output_msgs.append(self.data)
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        advance_by = 8
        self.local_node.on_bytes_sent(self.remote_fileno, advance_by)
        self.assertEqual(advance_by, self.connection.outputbuf.index)

    @patch("bxcommon.connections.abstract_node.AlarmQueue.time_to_next_alarm", return_value=(10, -1))
    @patch("bxcommon.connections.abstract_node.AlarmQueue.fire_ready_alarms", return_value=40)
    def test_get_sleep_timeout(self, mocked_time_to_next_alarm, mocked_fire_ready_alarms):
        self.assertEqual(DEFAULT_SLEEP_TIMEOUT,
                         self.local_node.get_sleep_timeout(triggered_by_timeout=10, first_call=True))
        self.assertEqual(40, self.local_node.get_sleep_timeout(triggered_by_timeout=10))
        self.local_node.connection_queue.append(self.connection)
        self.assertEqual(DEFAULT_SLEEP_TIMEOUT, self.local_node.get_sleep_timeout(triggered_by_timeout=10))
        mocked_fire_ready_alarms.assert_called()

    def test_close(self):
        self.assertNotIn(self.connection, self.local_node.connection_pool.by_fileno)
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.assertIn(self.connection, self.local_node.connection_pool.by_fileno)
        self.local_node.close()
        self.assertNotIn(self.connection, self.local_node.connection_pool.by_fileno)

    def test_enqueue_connection(self):
        self.assertNotIn((self.remote_ip, self.remote_port), self.local_node.connection_queue)
        self.local_node.enqueue_connection(self.remote_ip, self.remote_port)
        self.assertIn((self.remote_ip, self.remote_port), self.local_node.connection_queue)

    def test_enqueue_disconnect(self):
        self.assertNotIn(self.remote_fileno, self.local_node.disconnect_queue)
        self.local_node.enqueue_disconnect(self.remote_fileno)
        self.assertIn(self.remote_fileno, self.local_node.disconnect_queue)

    def test_pop_next_connection_address(self):
        self.assertIsNone(self.local_node.pop_next_connection_address())
        self.local_node.connection_queue.append((self.remote_ip, self.remote_port))
        self.assertEqual((self.remote_ip, self.remote_port), self.local_node.pop_next_connection_address())
        self.assertIsNone(self.local_node.pop_next_connection_address())

    def test_pop_next_disconnect_connection(self):
        self.assertIsNone(self.local_node.pop_next_disconnect_connection())
        self.local_node.disconnect_queue.append(self.remote_fileno)
        self.assertEqual(self.remote_fileno, self.local_node.pop_next_disconnect_connection())
        self.assertIsNone(self.local_node.pop_next_disconnect_connection())

    @patch("bxcommon.connections.abstract_node.SocketConnection.fileno", return_value=5)
    def test_initialize_connection(self, mock_fileno):
        socket_connection = MockSocketConnection(self.remote_fileno, self.remote_node)
        self.assertEqual(6, self.local_node.alarm_queue.uniq_count)
        self.assertIsNone(self.local_node.connection_pool.by_fileno[self.remote_fileno])
        self.local_node._initialize_connection(socket_connection, self.remote_ip, self.remote_port, True)
        self.assertEqual(7, self.local_node.alarm_queue.uniq_count)
        self.assertEqual(self.connection.fileno,
                         self.local_node.connection_pool.by_fileno[self.remote_fileno].fileno)

    @patch("bxcommon.connections.abstract_node.AlarmQueue.register_alarm")
    def test_connection_timeout_established(self, mocked_register_alarm):
        self.connection.state = ConnectionState.ESTABLISHED
        self.assertEqual(0, self.local_node._connection_timeout(self.connection))
        mocked_register_alarm.assert_not_called()
        self.connection.state = ConnectionState.MARK_FOR_CLOSE
        self.assertEqual(0, self.local_node._connection_timeout(self.connection))

    @patch("bxcommon.connections.abstract_node.AbstractNode.destroy_conn")
    def test_connection_timeout_connecting(self, mocked_destroy_conn):
        self.connection.state = ConnectionState.CONNECTING
        self.assertEqual(0, self.local_node._connection_timeout(self.connection))
        mocked_destroy_conn.assert_called_with(self.connection, retry_connection=True)

    def test_kill_node(self):
        with self.assertRaises(TerminationError):
            self.local_node._kill_node(None, None)

    def test_get_next_retry_timeout(self):
        self.connection.CONNECTION_TYPE = ConnectionType.BLOCKCHAIN_NODE
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)

        self.assertEqual(1, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(1, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(2, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(3, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(5, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(8, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(13, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        # caps at 13
        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 1
        self.assertEqual(13, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] += 10
        self.assertEqual(13, self.local_node._get_next_retry_timeout(self.remote_ip, self.remote_port))

    @patch("bxcommon.connections.abstract_node.AlarmQueue.register_alarm")
    def test_destroy_conn(self, mocked_register_alarm):
        self.connection.CONNECTION_TYPE = ConnectionType.BLOCKCHAIN_NODE
        self.connection.state = ConnectionState.MARK_FOR_CLOSE
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.local_node.destroy_conn(self.connection)
        mocked_register_alarm.assert_not_called()
        self.local_node.connection_pool.add(self.remote_fileno, self.remote_ip, self.remote_port, self.connection)
        self.local_node.destroy_conn(self.connection, retry_connection=True)
        self.assertIn(self.connection.fileno, self.local_node.disconnect_queue)
        mocked_register_alarm.assert_called_with(1, self.local_node._retry_init_client_socket,
                                                 self.remote_ip, self.remote_port, self.connection.CONNECTION_TYPE)

    @patch("bxcommon.connections.abstract_node.sdn_http_service.submit_peer_connection_error_event")
    def test_retry_init_client_socket(self, mocked_submit_peer):
        self.assertEqual(0, self.local_node._retry_init_client_socket(self.remote_ip, self.remote_port,
                                                                      ConnectionType.RELAY_ALL))
        self.assertIn((self.remote_ip, self.remote_port), self.local_node.connection_queue)
        self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)] = MAX_CONNECT_RETRIES
        self.local_node._retry_init_client_socket(self.remote_ip, self.remote_port, ConnectionType.RELAY_ALL)
        self.assertEqual(0, self.local_node.num_retries_by_ip[(self.remote_ip, self.remote_port)])
        mocked_submit_peer.assert_called_with(self.local_node.opts.node_id, self.remote_ip, self.remote_port)

    @patch("bxcommon.connections.abstract_node.AlarmQueue.register_alarm")
    def test_init_throughput_logging(self, mocked_alarm_queue):
        self.local_node.init_throughput_logging()
        mocked_alarm_queue.assert_called_once_with(FIRST_STATS_INTERVAL_S, throughput_statistics.flush_info)

    @patch("bxcommon.connections.abstract_node.memory_logger")
    def test_dump_memory_usage(self, logger_mock):
        # set to dump memory at 10 MB
        self.local_node.next_report_mem_usage_bytes = 10 * 1024 * 1024

        # current memory usage is 5 MB
        memory_utils.get_app_memory_usage = MagicMock(return_value=5 * 1024 * 1024)

        self.local_node.dump_memory_usage()
        # expect that memory details are not logged
        self.assertEqual(10 * 1024 * 1024, self.local_node.next_report_mem_usage_bytes)
        logger_mock.assert_not_called()

        # current memory usage goes up to 11 MB
        memory_utils.get_app_memory_usage = MagicMock(return_value=11 * 1024 * 1024)

        self.local_node.dump_memory_usage()
        # expect that memory details are logged
        self.assertEqual(11 * 1024 * 1024 + constants.MEMORY_USAGE_INCREASE_FOR_NEXT_REPORT_BYTES,
                         self.local_node.next_report_mem_usage_bytes)
        logger_mock.statistics.assert_called_once()

        # current memory usage goes up to 15 MB
        memory_utils.get_app_memory_usage = MagicMock(return_value=15 * 1024 * 1024)

        self.local_node.dump_memory_usage()
        # expect that memory details are not logged again
        self.assertEqual(11 * 1024 * 1024 + constants.MEMORY_USAGE_INCREASE_FOR_NEXT_REPORT_BYTES,
                         self.local_node.next_report_mem_usage_bytes)
        logger_mock.statistics.assert_called_once()


class TestNode(AbstractNode):
    NODE_TYPE = "TestNode"

    def __init__(self, opts):
        super(TestNode, self).__init__(opts)

    def get_outbound_peer_addresses(self):
        return True

    def send_request_for_peers(self):
        pass

    def build_connection(self, socket_connection, ip, port, from_me=False):
        return MockConnection(socket_connection, (ip, port), self, from_me)
