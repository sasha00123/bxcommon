from bxcommon.messages.bloxroute.bloxroute_message_factory import _BloxrouteMessageFactory
from bxcommon.messages.bloxroute.ping_message import PingMessage
from bxcommon.messages.bloxroute.pong_message import PongMessage
from bxcommon.test_utils.abstract_test_case import AbstractTestCase


class PingPongMessageTests(AbstractTestCase):
    def setUp(self):
        self.message_factory = _BloxrouteMessageFactory()
        self.message_factory._MESSAGE_TYPE_MAPPING = {
            "pong": PongMessage,
            "ping": PingMessage
        }

    def test_ping_message(self):
        self._test_message("ping", PingMessage)

    def test_pong_message(self):
        self._test_message("pong", PongMessage)

    def _test_message(self, msg_type, msg_cls):
        msg = msg_cls()

        self.assertTrue(msg)
        self.assertEqual(msg.msg_type(), msg_type)
        self.assertEqual(msg.payload_len(), 0)

        ping_msg_bytes = msg.rawbytes()
        self.assertTrue(ping_msg_bytes)

        parsed_ping_message = self.message_factory.create_message_from_buffer(ping_msg_bytes)

        self.assertIsInstance(parsed_ping_message, msg_cls)

        self.assertEqual(parsed_ping_message.msg_type(), msg_type)
        self.assertEqual(parsed_ping_message.payload_len(), 0)