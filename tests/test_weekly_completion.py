import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from stock_market_tracking_system import (
    ReportDataNotReadyError,
    TAIPEI_TZ,
    last_twse_trading_day_of_week,
    latest_twse_trading_day,
    load_schedule_rules,
    parse_twse_closed_dates,
    resolve_report_target,
    resolve_weekly_report_target,
    validate_complete_report_results,
    weekly_report_start_time,
)


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

        with self.assertRaisesRegex(ReportDataNotReadyError, "缺少 台積電"):
            validate_complete_report_results(results, self.watchlist, self.expected_date)

    def test_rejects_stale_stock(self):
        results = [
            ("加權指數", "^TWII", {"data_date": self.expected_date}),
            ("台積電", "2330.TW", {"data_date": "2026-06-04"}),
        ]

        with self.assertRaisesRegex(ReportDataNotReadyError, "資料日不符 台積電"):
            validate_complete_report_results(results, self.watchlist, self.expected_date)

    def test_duplicate_ticker_remains_a_hard_failure(self):
        results = [
            ("加權指數", "^TWII", {"data_date": self.expected_date}),
            ("台積電", "2330.TW", {"data_date": self.expected_date}),
            ("台積電", "2330.TW", {"data_date": self.expected_date}),
        ]

        with self.assertRaisesRegex(RuntimeError, "重複標的 2330.TW") as caught:
            validate_complete_report_results(results, self.watchlist, self.expected_date)
        self.assertNotIsInstance(caught.exception, ReportDataNotReadyError)


class WeeklyTradingDateTests(unittest.TestCase):
    def test_holiday_calendar_excludes_named_trading_days(self):
        payload = {
            "data": [
                ["2026-02-11", "農曆春節前最後交易日", ""],
                ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
                ["2026-02-20", "農曆除夕及春節", ""],
                ["bad-date", "測試休市", ""],
            ]
        }

        self.assertEqual(
            parse_twse_closed_dates(payload),
            {date(2026, 2, 12), date(2026, 2, 20)},
        )

    def test_friday_holiday_moves_report_day_to_thursday(self):
        closed = {date(2026, 6, 19)}

        self.assertEqual(
            last_twse_trading_day_of_week(date(2026, 6, 19), closed),
            date(2026, 6, 18),
        )

    def test_report_becomes_due_on_holiday_shortened_week(self):
        closed = {date(2026, 6, 19)}
        now = datetime(2026, 6, 18, 15, 0, tzinfo=TAIPEI_TZ)

        self.assertEqual(resolve_weekly_report_target(now, closed), date(2026, 6, 18))

    def test_weekly_run_after_comes_from_schedule_rules(self):
        rules = {"profiles": {"weekly": {"run_after": "16:00"}}}
        now = datetime(2026, 6, 5, 15, 30, tzinfo=TAIPEI_TZ)

        self.assertEqual(weekly_report_start_time(rules).hour, 16)
        self.assertEqual(resolve_weekly_report_target(now, set(), run_after=weekly_report_start_time(rules)), date(2026, 5, 29))

    def test_before_current_week_final_day_keeps_previous_target(self):
        now = datetime(2026, 6, 18, 15, 0, tzinfo=TAIPEI_TZ)

        self.assertEqual(resolve_weekly_report_target(now, set()), date(2026, 6, 12))

    def test_retry_target_continues_after_23_and_across_days(self):
        now = datetime(2026, 6, 6, 2, 0, tzinfo=TAIPEI_TZ)

        self.assertEqual(resolve_weekly_report_target(now, set()), date(2026, 6, 5))

    def test_force_run_uses_latest_complete_week_not_action_day(self):
        now = datetime(2026, 6, 23, 16, 0, tzinfo=TAIPEI_TZ)

        with patch("weekly_data_sources.fetch_twse_closed_dates", return_value=set()):
            self.assertEqual(resolve_report_target(now, force_run=True), date(2026, 6, 19))

    def test_full_week_holiday_keeps_previous_target(self):
        closed = {date(2026, 2, day) for day in range(16, 21)}
        now = datetime(2026, 2, 20, 18, 0, tzinfo=TAIPEI_TZ)

        self.assertEqual(resolve_weekly_report_target(now, closed), date(2026, 2, 13))

    def test_latest_trading_day_skips_holiday(self):
        closed = {date(2026, 6, 19)}
        now = datetime(2026, 6, 19, 16, 0, tzinfo=TAIPEI_TZ)

        self.assertEqual(latest_twse_trading_day(now, closed), date(2026, 6, 18))

    def test_force_run_falls_back_to_latest_weekday_when_calendar_is_temporarily_unavailable(self):
        now = datetime(2026, 6, 6, 10, 0, tzinfo=TAIPEI_TZ)

        with patch(
            "weekly_data_sources.fetch_twse_closed_dates",
            side_effect=RuntimeError("TWSE calendar unavailable"),
        ):
            self.assertEqual(resolve_report_target(now, force_run=True), date(2026, 6, 5))

    def test_load_schedule_rules_reads_external_rules_file(self):
        rules_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "schedule_rules_weekly.json"

        self.assertEqual(
            load_schedule_rules(str(rules_path))["profiles"]["weekly"]["run_after"],
            "15:00",
        )


if __name__ == "__main__":
    unittest.main()
