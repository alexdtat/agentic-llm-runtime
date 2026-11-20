import unittest

from dispatch import plan_orders
from warehouse.samples import full_samples


class BatchTests(unittest.TestCase):
    def test_exact_multiple(self):
        self.assertEqual(plan_orders(["a", "b", "c", "d"], 2), [["a", "b"], ["c", "d"]])

    def test_partial_batch(self):
        self.assertEqual(plan_orders(["a", "b", "c"], 2), [["a", "b"], ["c"]])

    def test_warehouse_samples(self):
        self.assertEqual(full_samples(["a", "b", "c"], 2), [["a", "b"]])
