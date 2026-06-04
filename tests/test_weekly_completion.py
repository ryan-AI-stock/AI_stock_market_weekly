import unittest

from stock_market_tracking_system import validate_complete_report_results


class ValidateCompleteReportResultsTests(unittest.TestCase):
    def setUp(self):
        self.watchlist = [
            {"name": "加權指數", "ticker": "^TWII"},
            {"name": "台積電", "ticker": "2330.TW"},
        ]
        self.expected_date = "2026-06-05"

    def test_accepts_complete_current_results(self):
        results = [
            ("加權指數", "^TWII", {"data_date": self.expected_date}),
            ("台積電", "2330.TW", {"data_date": self.expected_date}),
        ]

        validate_complete_report_results(results, self.watchlist, self.expected_date)

    def test_rejects_missing_stock(self):
        results = [
            ("加權指數", "^TWII", {"data_date": self.expected_date}),
        ]

        with self.assertRaisesRegex(RuntimeError, "缺少 台積電"):
            validate_complete_report_results(results, self.watchlist, self.expected_date)

    def test_rejects_stale_stock(self):
        results = [
            ("加權指數", "^TWII", {"data_date": self.expected_date}),
            ("台積電", "2330.TW", {"data_date": "2026-06-04"}),
        ]

        with self.assertRaisesRegex(RuntimeError, "資料日不符 台積電"):
            validate_complete_report_results(results, self.watchlist, self.expected_date)


if __name__ == "__main__":
    unittest.main()
