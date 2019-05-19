from collections import defaultdict, deque
from typing import List, Tuple

from bxcommon import constants
from bxcommon.models.transaction_info import TransactionSearchResult, TransactionInfo
from bxcommon.utils import logger, memory_utils, convert
from bxcommon.utils.expiration_queue import ExpirationQueue
from bxcommon.utils.memory_utils import ObjectSize
from bxcommon.utils.object_hash import Sha256Hash
from bxcommon.utils.stats import hooks


class TransactionService(object):
    """
    Service for managing transaction mappings.
    In this class, we assume that no more than MAX_ID unassigned transactions exist at a time.

    Constants
    ---------
    MAX_ID: maximum short id value (e.g. number of bits in a short id)
    SHORT_ID_SIZE: number of bytes in a short id, must match TxMessage


    Attributes
    ----------
    node: reference to node holding transaction service reference
    tx_assign_alarm_scheduled: if an alarm to expire a batch of short ids is currently active
    network_num: network number that current transaction service serves
    _tx_hash_to_short_ids: mapping of transaction long hashes to (potentially multiple) short ids
    _short_id_to_tx_hash: mapping of short id to transaction long hashes
    _tx_hash_to_contents: mapping of transaction long hashes to transaction contents
    _tx_assignment_expire_queue: expiration time of short ids
    """

    MAX_ID = 2 ** 32
    SHORT_ID_SIZE = 4
    DEFAULT_FINAL_TX_CONFIRMATIONS_COUNT = 24

    ESTIMATED_TX_HASH_AND_SHORT_ID_ITEM_SIZE = 376
    ESTIMATED_TX_HASH_ITEM_SIZE = 312
    ESTIMATED_SHORT_ID_EXPIRATION_ITEM_SIZE = 88

    def __init__(self, node, network_num):
        """
        Constructor
        :param node: reference to node object
        :param network_num: network number
        """

        if node is None:
            raise ValueError("Node is required")

        if network_num is None:
            raise ValueError("Network number is required")

        self.node = node
        self.network_num = network_num

        self.tx_assign_alarm_scheduled = False

        self._tx_hash_to_short_ids = defaultdict(set)
        self._short_id_to_tx_hash = {}
        self._tx_hash_to_contents = {}
        self._tx_assignment_expire_queue = ExpirationQueue(node.opts.sid_expire_time)

        self._final_tx_confirmations_count = self._get_final_tx_confirmations_count()
        self._tx_content_memory_limit = self._get_tx_contents_memory_limit()
        logger.info("Memory limit for transaction service by network number {} is {} bytes.".format(self.network_num,
                                                                                                    self._tx_content_memory_limit))

        # deque of short ids in blocks in the order they are received
        self._short_ids_seen_in_block = deque()

        self._total_tx_contents_size = 0
        self._total_tx_removed_by_memory_limit = 0

    def set_transaction_contents(self, transaction_hash, transaction_contents):
        """
        Adds transaction contents to transaction service cache with lookup key by transaction hash

        :param transaction_hash: transaction hash
        :param transaction_contents: transaction contents bytes
        """
        previous_size = 0
        transaction_cache_key = self._tx_hash_to_cache_key(transaction_hash)

        if transaction_cache_key in self._tx_hash_to_contents:
            previous_size = len(self._tx_hash_to_contents[transaction_cache_key])

        self._tx_hash_to_contents[transaction_cache_key] = transaction_contents
        self._total_tx_contents_size += len(transaction_contents) - previous_size

        self._memory_limit_clean_up()

    def has_transaction_contents(self, transaction_hash):
        """
        Checks if transaction contents is available in transaction service cache

        :param transaction_hash: transaction hash
        :return: Boolean indicating if transaction contents exists
        """
        return self._tx_hash_to_cache_key(transaction_hash) in self._tx_hash_to_contents

    def has_transaction_short_id(self, transaction_hash):
        """
        Checks if transaction short id is available in transaction service cache

        :param transaction_hash: transaction hash
        :return: Boolean indicating if transaction short id exists
        """
        return self._tx_hash_to_cache_key(transaction_hash) in self._tx_hash_to_short_ids

    def has_short_id(self, short_id):
        """
        Checks if short id is stored in transaction service cache
        :param short_id: transaction short id
        :return: Boolean indicating if short id is found in cache
        """
        return short_id in self._short_id_to_tx_hash

    def assign_short_id(self, transaction_hash, short_id):
        """
        Adds short id mapping for transaction and schedules an alarm to cleanup entry on expiration.
        :param transaction_hash: transaction long hash
        :param short_id: short id to be mapped to transaction
        """
        if short_id == constants.NULL_TX_SID:
            logger.warn("Attempt to assign null SID to transaction hash {}. Ignoring.", transaction_hash)
            return
        logger.debug("Assigning sid {} to transaction {}", short_id, transaction_hash)

        transaction_cache_key = self._tx_hash_to_cache_key(transaction_hash)
        self._tx_hash_to_short_ids[transaction_cache_key].add(short_id)
        self._short_id_to_tx_hash[short_id] = transaction_cache_key
        self._tx_assignment_expire_queue.add(short_id)

        if not self.tx_assign_alarm_scheduled:
            self.node.alarm_queue.register_alarm(self.node.opts.sid_expire_time, self.expire_old_assignments)
            self.tx_assign_alarm_scheduled = True

    def get_short_id(self, transaction_hash):
        """
        Fetches a single short id for transaction. If the transaction has multiple short id mappings, just gets
        the first one.
        :param transaction_hash: transaction long hash
        :return: short id
        """
        return next(iter(self.get_short_ids(transaction_hash)))

    def get_short_ids(self, transaction_hash):
        """
        Fetches all short ids for a given transactions
        :param transaction_hash: transaction long hash
        :return: set of short ids
        """
        transaction_cache_key = self._tx_hash_to_cache_key(transaction_hash)

        if transaction_cache_key in self._tx_hash_to_short_ids:
            return self._tx_hash_to_short_ids[transaction_cache_key]
        else:
            return {constants.NULL_TX_SID}

    def get_transaction(self, short_id: int) -> TransactionInfo:
        """
        Fetches transaction info for a given short id.
        Results might be None.
        :param short_id:
        :return: transaction hash, transaction contents.
        """
        if short_id in self._short_id_to_tx_hash:
            transaction_hash = self._short_id_to_tx_hash[short_id]
            transaction_cache_key = self._tx_hash_to_cache_key(transaction_hash)
            if transaction_cache_key in self._tx_hash_to_contents:
                transaction_contents = self._tx_hash_to_contents[transaction_cache_key]
                return TransactionInfo(self._tx_cache_key_to_hash(transaction_cache_key),
                                       transaction_contents,
                                       short_id)
            else:
                return TransactionInfo(self._tx_cache_key_to_hash(transaction_cache_key), None, short_id)
        else:
            return TransactionInfo(None, None, short_id)

    def get_missing_transactions(
            self, short_ids: List[int]
    ) -> Tuple[bool, List[int], List[Sha256Hash]]:
        unknown_tx_sids = []
        unknown_tx_hashes = []
        has_missing = False
        for short_id in short_ids:
            transaction_hash = self._short_id_to_tx_hash.get(short_id, None)
            if transaction_hash is None:
                unknown_tx_sids.append(short_id)
                has_missing = True
            elif not self.has_transaction_contents(transaction_hash):
                unknown_tx_hashes.append(self._tx_cache_key_to_hash(transaction_hash))
                has_missing = True
        return has_missing, unknown_tx_sids, unknown_tx_hashes

    def get_transaction_by_hash(self, transaction_hash):
        """
        Fetches transaction contents for a given transaction hash.
        Results might be None.
        :param transaction_hash: transaction hash
        :return: transaction contents.
        """
        transaction_cache_key = self._tx_hash_to_cache_key(transaction_hash)
        if transaction_cache_key in self._tx_hash_to_contents:
            return self._tx_hash_to_contents[transaction_cache_key]

        return None

    def get_transactions(self, short_ids: List[int]) -> TransactionSearchResult:
        """
        Fetches all transaction info for a set of short ids.
        Short ids without a transaction entry will be omitted.
        :param short_ids: list of short ids
        :return: list of found and missing short ids
        """
        found = []
        missing = []
        for short_id in short_ids:
            if short_id in self._short_id_to_tx_hash:
                transaction_cache_key = self._short_id_to_tx_hash[short_id]
                if transaction_cache_key in self._tx_hash_to_contents:
                    found.append(TransactionInfo(self._tx_cache_key_to_hash(transaction_cache_key),
                                                 self._tx_hash_to_contents[transaction_cache_key],
                                                 short_id))
                else:
                    missing.append(TransactionInfo(None, None, short_id))
                    logger.debug("Short id {} was requested but is unknown.", short_id)
            else:
                logger.debug("Short id {} was requested but is unknown.", short_id)

        return TransactionSearchResult(found, missing)

    def expire_old_assignments(self):
        """
        Clean up expired short ids.
        """
        logger.info(
            "Expiring old short id assignments. Total entries: {}".format(len(self._tx_assignment_expire_queue)))
        self._tx_assignment_expire_queue.remove_expired(remove_callback=self._remove_transaction_by_short_id)
        logger.info(
            "Finished cleaning up short ids. Entries remaining: {}".format(len(self._tx_assignment_expire_queue)))
        if len(self._tx_assignment_expire_queue) > 0:
            return self.node.opts.sid_expire_time
        else:
            self.tx_assign_alarm_scheduled = False
            return 0

    def track_seen_short_ids(self, short_ids):
        """
        Track short ids that has been seen in a routed block.
        That information helps transaction service make a decision when to remove transactions from cache.

        :param short_ids: transaction short ids
        """

        if short_ids is None:
            return ValueError("short_ids is required.")

        self._short_ids_seen_in_block.append(short_ids)

        if len(self._short_ids_seen_in_block) > self._final_tx_confirmations_count:

            final_short_ids = self._short_ids_seen_in_block.popleft()

            for short_id in final_short_ids:
                self._remove_transaction_by_short_id(short_id)

    def log_tx_service_mem_stats(self):
        """
        Logs transactions service memory statistics
        """

        class_name = self.__class__.__name__
        hooks.add_obj_mem_stats(
            class_name,
            self.network_num,
            self._tx_hash_to_short_ids,
            "tx_hash_to_short_ids",
            self.get_collection_mem_stats(self._tx_hash_to_short_ids,
                                          self.ESTIMATED_TX_HASH_AND_SHORT_ID_ITEM_SIZE * len(
                                              self._tx_hash_to_short_ids)))

        hooks.add_obj_mem_stats(
            class_name,
            self.network_num,
            self._tx_hash_to_contents,
            "tx_hash_to_contents",
            self.get_collection_mem_stats(self._tx_hash_to_contents, self.ESTIMATED_TX_HASH_ITEM_SIZE * len(
                self._tx_hash_to_contents) + self._total_tx_contents_size))

        hooks.add_obj_mem_stats(
            class_name,
            self.network_num,
            self._short_id_to_tx_hash,
            "short_id_to_tx_hash",
            self.get_collection_mem_stats(self._short_id_to_tx_hash,
                                          self.ESTIMATED_TX_HASH_AND_SHORT_ID_ITEM_SIZE * len(
                                              self._short_id_to_tx_hash)))

        hooks.add_obj_mem_stats(
            class_name,
            self.network_num,
            self._short_ids_seen_in_block,
            "short_ids_seen_in_block",
            self.get_collection_mem_stats(self._short_ids_seen_in_block, 0))

        hooks.add_obj_mem_stats(
            class_name,
            self.network_num,
            self._tx_assignment_expire_queue,
            "tx_assignment_expire_queue",
            self.get_collection_mem_stats(self._tx_assignment_expire_queue,
                                          self.ESTIMATED_SHORT_ID_EXPIRATION_ITEM_SIZE * len(
                                              self._tx_assignment_expire_queue)))

    def get_tx_service_aggregate_stats(self):
        """
        Returns dictionary with aggregated statistics of transactions service

        :return: dictionary with aggregated statistics
        """
        oldest_transaction_date = 0
        oldest_transaction_hash = ""

        if len(self._tx_assignment_expire_queue.queue) > 0:
            oldest_transaction_date = self._tx_assignment_expire_queue.get_oldest_item_timestamp()
            oldest_transaction_sid = self._tx_assignment_expire_queue.get_oldest()
            if oldest_transaction_sid in self._short_id_to_tx_hash:
                oldest_transaction_hash = self._short_id_to_tx_hash[oldest_transaction_sid]

        return dict(
            short_id_mapping_count_gauge=len(self._short_id_to_tx_hash),
            unique_transaction_content_gauge=len(self._tx_hash_to_contents),
            oldest_transaction_date=oldest_transaction_date,
            oldest_transaction_hash=oldest_transaction_hash,
            transactions_removed_by_memory_limit=self._total_tx_removed_by_memory_limit,
            total_tx_contents_size=self._total_tx_contents_size
        )

    def get_collection_mem_stats(self, collection_obj, estimated_size=0):
        if self.node.opts.stats_calculate_actual_size:
            return memory_utils.get_object_size(collection_obj)
        else:
            return ObjectSize(size=estimated_size, flat_size=0, is_actual_size=False)

    def _remove_transaction_by_short_id(self, short_id):
        """
        Clean up short id mapping. Removes transaction contents and mapping if only one short id mapping.
        :param short_id: short id to clean up
        """
        if short_id in self._short_id_to_tx_hash:
            transaction_cache_key = self._short_id_to_tx_hash.pop(short_id)
            if transaction_cache_key in self._tx_hash_to_short_ids:
                short_ids = self._tx_hash_to_short_ids[transaction_cache_key]

                # Only clear mapping and txhash_to_contents if last SID assignment
                if len(short_ids) == 1:
                    del self._tx_hash_to_short_ids[transaction_cache_key]

                    if transaction_cache_key in self._tx_hash_to_contents:
                        self._total_tx_contents_size -= len(self._tx_hash_to_contents[transaction_cache_key])
                        del self._tx_hash_to_contents[transaction_cache_key]
                else:
                    short_ids.remove(short_id)

        self._tx_assignment_expire_queue.remove(short_id)

    def _memory_limit_clean_up(self):
        """
        Removes oldest transactions if total bytes consumed by transaction contents exceed memory limit
        """
        if self._total_tx_contents_size <= self._tx_content_memory_limit:
            return

        logger.debug("Transaction service exceeds memory limit for transaction contents. Limit: {}. Current size: {}."
                     .format(self._tx_content_memory_limit, self._total_tx_contents_size))
        removed_tx_count = 0

        while self._total_tx_contents_size > self._tx_content_memory_limit:
            self._tx_assignment_expire_queue.remove_oldest(remove_callback=self._remove_transaction_by_short_id)
            removed_tx_count += 1

        self._total_tx_removed_by_memory_limit += removed_tx_count
        logger.debug("Removed {} oldest transactions from transaction service cache. Size after clean up: {}".format(
            removed_tx_count, self._total_tx_contents_size))

    def _get_final_tx_confirmations_count(self):
        """
        Returns configuration value of number of block confirmations required before transaction can be removed
        """
        for blockchain_network in self.node.opts.blockchain_networks:
            if blockchain_network.network_num == self.network_num:
                return blockchain_network.final_tx_confirmations_count

        logger.warn("Tx service could not determine final confirmations count for network number {}. Using default {}."
                    .format(self.network_num, self.DEFAULT_FINAL_TX_CONFIRMATIONS_COUNT))

        return self.DEFAULT_FINAL_TX_CONFIRMATIONS_COUNT

    def _get_tx_contents_memory_limit(self):
        """
        Returns configuration value for memory limit for total transaction contents
        """
        if self.node.opts.transaction_pool_memory_limit is not None:
            # convert MB to bytes
            return self.node.opts.transaction_pool_memory_limit * 1024 * 1024

        for blockchain_network in self.node.opts.blockchain_networks:
            if blockchain_network.network_num == self.network_num:
                if blockchain_network.tx_contents_memory_limit_bytes is None:
                    logger.warn(
                        "Blockchain network by number {} does not have tx cache size limit configured. Using default {}."
                            .format(self.network_num, constants.DEFAULT_TX_CACHE_MEMORY_LIMIT_BYTES))
                    return constants.DEFAULT_TX_CACHE_MEMORY_LIMIT_BYTES
                else:
                    return blockchain_network.tx_contents_memory_limit_bytes

        logger.warn("Tx service could not determine transactions memory limit for network number {}. Using default {}."
                    .format(self.network_num, constants.DEFAULT_TX_CACHE_MEMORY_LIMIT_BYTES))
        return constants.DEFAULT_TX_CACHE_MEMORY_LIMIT_BYTES

    def _tx_hash_to_cache_key(self, transaction_hash):

        if isinstance(transaction_hash, Sha256Hash):
            return convert.bytes_to_hex(transaction_hash.binary)

        if isinstance(transaction_hash, (bytes, bytearray, memoryview)):
            return convert.bytes_to_hex(transaction_hash)

        return transaction_hash

    def _tx_cache_key_to_hash(self, transaction_cache_key):
        if isinstance(transaction_cache_key, Sha256Hash):
            return transaction_cache_key

        if isinstance(transaction_cache_key, (bytes, bytearray, memoryview)):
            return Sha256Hash(transaction_cache_key)

        return Sha256Hash(convert.hex_to_bytes(transaction_cache_key))
