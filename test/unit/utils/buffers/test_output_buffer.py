import unittest
from collections import deque

from bxcommon.utils.buffers.output_buffer import OutputBuffer


class TestOutputBuffer(unittest.TestCase):
    def setUp(self):
        self.out_buf = OutputBuffer()

    def test_get_buffer(self):
        with self.assertRaises(RuntimeError):
            self.out_buf.get_buffer()

        data1 = bytearray([i for i in range(20)])
        self.out_buf.enqueue_msgbytes(data1)
        self.assertEqual(data1, self.out_buf.get_buffer())

        data2 = bytearray([i for i in range(20, 40)])
        self.out_buf.enqueue_msgbytes(data2)
        self.assertEqual(data1, self.out_buf.get_buffer())

        new_index = 10
        self.out_buf.index = new_index
        self.assertEqual(data1[new_index:], self.out_buf.get_buffer())

    def test_advance_buffer(self):
        with self.assertRaises(ValueError):
            self.out_buf.advance_buffer(5)

        data1 = bytearray([i for i in range(20)])
        self.out_buf.enqueue_msgbytes(data1)
        data2 = bytearray([i for i in range(20, 40)])
        self.out_buf.enqueue_msgbytes(data2)

        self.out_buf.advance_buffer(10)
        self.assertEqual(10, self.out_buf.index)
        self.assertEqual(30, self.out_buf.length)

        self.out_buf.advance_buffer(10)
        self.assertEqual(0, self.out_buf.index)
        self.assertEqual(1, len(self.out_buf.output_msgs))

    def test_at_msg_boundary(self):
        self.assertTrue(self.out_buf.at_msg_boundary())
        self.out_buf.index = 1
        self.assertFalse(self.out_buf.at_msg_boundary())

    def test_enqueue_msgbytes(self):
        with self.assertRaises(ValueError):
            self.out_buf.enqueue_msgbytes("f")

        data1 = bytearray([i for i in range(20)])
        self.out_buf.enqueue_msgbytes(data1)
        self.assertEqual(data1, self.out_buf.get_buffer())

        data2 = bytearray([i for i in range(20, 40)])
        self.out_buf.enqueue_msgbytes(data2)
        self.assertEqual(data1, self.out_buf.get_buffer())

        new_index = 10
        self.out_buf.index = new_index
        self.assertEqual(data1[new_index:], self.out_buf.get_buffer())

    def test_prepend_msg(self):
        with self.assertRaises(ValueError):
            self.out_buf.prepend_msg("f")

        data1 = bytearray([i for i in range(20)])
        self.out_buf.prepend_msg(data1)

        data2 = bytearray([i for i in range(20, 40)])
        self.out_buf.prepend_msg(data2)

        confirm1 = deque()
        confirm1.append(data2)
        confirm1.append(data1)

        self.assertEqual(confirm1, self.out_buf.output_msgs)
        self.assertEqual(40, self.out_buf.length)

        self.out_buf.advance_buffer(10)

        data3 = bytearray([i for i in range(40, 60)])
        self.out_buf.prepend_msg(data3)

        confirm2 = deque()
        confirm2.append(data2)
        confirm2.append(data3)
        confirm2.append(data1)

        self.assertEqual(confirm2, self.out_buf.output_msgs)
        self.assertEqual(50, self.out_buf.length)

    def test_has_more_bytes(self):
        self.assertFalse(self.out_buf.has_more_bytes())
        self.out_buf.length = 1
        self.assertTrue(self.out_buf.has_more_bytes())