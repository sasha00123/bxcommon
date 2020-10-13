import time

from bxcommon.messages.bloxroute.ack_message import AckMessage
from bxcommon.messages.bloxroute.bdn_performance_stats_message import BdnPerformanceStatsMessage
from bxcommon.messages.bloxroute.block_confirmation_message import BlockConfirmationMessage
from bxcommon.messages.bloxroute.broadcast_message import BroadcastMessage
from bxcommon.messages.bloxroute.get_txs_message import GetTxsMessage
from bxcommon.messages.bloxroute.hello_message import HelloMessage
from bxcommon.messages.bloxroute.key_message import KeyMessage
from bxcommon.messages.bloxroute.notification_message import NotificationMessage
from bxcommon.messages.bloxroute.ping_message import PingMessage
from bxcommon.messages.bloxroute.pong_message import PongMessage
from bxcommon.messages.bloxroute.transaction_cleanup_message import TransactionCleanupMessage
from bxcommon.messages.bloxroute.tx_message import TxMessage
from bxcommon.messages.bloxroute.tx_service_sync_blocks_short_ids_message import TxServiceSyncBlocksShortIdsMessage
from bxcommon.messages.bloxroute.tx_service_sync_complete_message import TxServiceSyncCompleteMessage
from bxcommon.messages.bloxroute.tx_service_sync_req_message import TxServiceSyncReqMessage
from bxcommon.messages.bloxroute.tx_service_sync_txs_message import TxServiceSyncTxsMessage
from bxcommon.messages.bloxroute.txs_message import TxsMessage
from bxcommon.messages.bloxroute.v14.bdn_performance_stats_message_v14 import \
    BdnPerformanceStatsMessageV14
from bxcommon.test_utils.abstract_bloxroute_version_manager_test import AbstractBloxrouteVersionManagerTest


class BloxrouteVersionManagerV14Test(
    AbstractBloxrouteVersionManagerTest[
        HelloMessage,
        AckMessage,
        PingMessage,
        PongMessage,
        BroadcastMessage,
        TxMessage,
        GetTxsMessage,
        TxsMessage,
        KeyMessage,
        TxServiceSyncReqMessage,
        TxServiceSyncBlocksShortIdsMessage,
        TxServiceSyncTxsMessage,
        TxServiceSyncCompleteMessage,
        BlockConfirmationMessage,
        TransactionCleanupMessage,
        NotificationMessage,
        BdnPerformanceStatsMessageV14,
    ]
):

    def version_to_test(self) -> int:
        return 14

    def old_bdn_performance_stats_message(
        self, original_message: BdnPerformanceStatsMessage
    ) -> BdnPerformanceStatsMessageV14:
        return BdnPerformanceStatsMessageV14(
            original_message.interval_start_time(),
            original_message.interval_end_time(),
            original_message.new_blocks_from_blockchain_node(),
            original_message.new_blocks_from_bdn(),
            original_message.new_tx_from_blockchain_node(),
            original_message.new_tx_from_bdn(),
            original_message.memory_utilization()
        )

    def compare_bdn_performance_stats_current_to_old(
        self,
        converted_old_message: BdnPerformanceStatsMessageV14,
        original_old_message: BdnPerformanceStatsMessageV14,
    ):
        self.assert_attributes_equal(
            original_old_message,
            converted_old_message,
            [
                "interval_start_time",
                "interval_end_time",
                "new_blocks_from_blockchain_node",
                "new_blocks_from_bdn",
                "new_tx_from_blockchain_node",
                "new_tx_from_bdn",
                "memory_utilization"
            ]
        )

    def compare_bdn_performance_stats_old_to_current(
        self,
        converted_current_message: BdnPerformanceStatsMessage,
        original_current_message: BdnPerformanceStatsMessage,
    ):

        self.assert_attributes_equal(
            converted_current_message,
            original_current_message,
            [
                "interval_start_time",
                "interval_end_time",
                "new_blocks_from_blockchain_node",
                "new_blocks_from_bdn",
                "new_tx_from_blockchain_node",
                "new_tx_from_bdn",
                "memory_utilization"
            ],
        )
        self.assertEqual(0, converted_current_message.new_blocks_seen())
        self.assertEqual(0, converted_current_message.new_block_messages_from_blockchain_node())
        self.assertEqual(0, converted_current_message.new_block_announcements_from_blockchain_node())