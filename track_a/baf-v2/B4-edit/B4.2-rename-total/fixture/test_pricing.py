import unittest
from billing.pricing import calc_total

class TotalTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(str(calc_total([10, 5])), "15.00")
