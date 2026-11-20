import unittest

from tracker import format_minutes


class FormattingTests(unittest.TestCase):
    def test_whole_hour(self):
        self.assertEqual(format_minutes(120), "2:00")

    def test_remaining_minutes(self):
        self.assertEqual(format_minutes(67), "1:07")
