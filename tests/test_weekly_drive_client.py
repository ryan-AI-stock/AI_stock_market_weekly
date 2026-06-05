import unittest

from weekly_drive_client import drive_name_query


class WeeklyDriveClientTests(unittest.TestCase):
    def test_drive_name_query_escapes_quote_and_backslash(self):
        self.assertEqual(
            drive_name_query(r"週報\2026's.pdf"),
            r"週報\\2026\'s.pdf",
        )


if __name__ == "__main__":
    unittest.main()
