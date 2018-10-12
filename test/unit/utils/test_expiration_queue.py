import time
import unittest

from bxcommon.utils.expiration_queue import ExpirationQueue


class ExpirationQueueTests(unittest.TestCase):
    def setUp(self):
        self.time_to_live = 60
        self.queue = ExpirationQueue(self.time_to_live)
        self.removed_items = []

    def test_expiration_queue(self):
        # adding 2 items to the queue with 1 second difference
        item1 = 1
        item2 = 2

        self.queue.add(item1)
        time_1_added = time.time()

        time.sleep(1)

        self.queue.add(item2)
        time_2_added = time.time()

        self.assertEqual(len(self.queue), 2)

        # check that nothing is removed from queue before the first item expires
        self.queue.remove_expired(time_1_added + self.time_to_live / 2, remove_callback=self._remove_item)
        self.assertEqual(len(self.queue), 2)
        self.assertEqual(len(self.removed_items), 0)

        # check that first item removed after first item expired
        self.queue.remove_expired(time_1_added + self.time_to_live + 1, remove_callback=self._remove_item)
        self.assertEqual(len(self.queue), 1)
        self.assertEqual(len(self.removed_items), 1)
        self.assertEqual(self.removed_items[0], item1)

        # check that second item is removed after second item expires
        self.queue.remove_expired(time_2_added + self.time_to_live + 1, remove_callback=self._remove_item)
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(len(self.removed_items), 2)
        self.assertEqual(self.removed_items[0], item1)
        self.assertEqual(self.removed_items[1], item2)

    def _remove_item(self, item):
        self.removed_items.append(item)