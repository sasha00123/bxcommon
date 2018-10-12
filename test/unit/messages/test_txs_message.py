from bxcommon.messages.txs_message import TxsMessage
from bxcommon.test_utils import helpers
from bxcommon.test_utils.abstract_test_case import AbstractTestCase
from bxcommon.utils.object_hash import ObjectHash


class TxsMessageTests(AbstractTestCase):

    def test_txs_with_short_ids_message(self):
        txs_info = [
            (111, ObjectHash(helpers.generate_bytearray(32)), helpers.generate_bytearray(200)),
            (222, ObjectHash(helpers.generate_bytearray(32)), helpers.generate_bytearray(300)),
            (333, ObjectHash(helpers.generate_bytearray(32)), helpers.generate_bytearray(400))
        ]

        msg = TxsMessage(txs=txs_info)

        msg_bytes = msg.rawbytes()

        self.assertTrue(msg_bytes)

        parsed_msg = TxsMessage(buf=msg_bytes)

        self.assertTrue(parsed_msg)

        parsed_txs_info = parsed_msg.get_txs()

        self.assertEqual(len(parsed_txs_info), len(txs_info))

        for index in range(len(txs_info)):
            self.assertEqual(parsed_txs_info[index][0], txs_info[index][0])
            self.assertEqual(parsed_txs_info[index][1], txs_info[index][1])
            self.assertEqual(parsed_txs_info[index][2], txs_info[index][2])