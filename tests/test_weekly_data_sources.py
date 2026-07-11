import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

import pandas as pd

from weekly_data_sources import (
    TAIPEI_TZ,
    _cumulative,
    _expected_latest_price_date,
    _is_fresh_price_data,
    _parse_float,
    _parse_int,
    fetch_twse_closed_dates,
)


class WeeklyDataSourceUtilityTests(unittest.TestCase):
    def test_holiday_calendar_uses_official_openapi_fallback(self):
        bad_response = Mock()
        bad_response.json.side_effect = ValueError("invalid json")
        fallback_dates = {date(2026, 2, 16)}

        with (
            patch("weekly_data_sources.requests.get", return_value=bad_response),
            patch("weekly_data_sources.fetch_twse_openapi_closed_dates", return_value=fallback_dates) as fallback,
        ):
            result = fetch_twse_closed_dates({2026})

        self.assertEqual(result, fallback_dates)
        fallback.assert_called_once_with({2026})

    def test_parse_number_helpers_tolerate_commas_and_empty_values(self):
        self.assertEqual(_parse_int("1,234 "), 1234)
        self.assertEqual(_parse_int("--"), 0)
        self.assertEqual(_parse_float(" 1,234.5 "), 1234.5)
        self.assertIsNone(_parse_float("--"))

    def test_expected_latest_price_date_uses_close_time_and_weekend_rules(self):
        self.assertEqual(
            _expected_latest_price_date(datetime(2026, 6, 5, 13, 39, tzinfo=TAIPEI_TZ)),
            date(2026, 6, 4),
        )
        self.assertEqual(
            _expected_latest_price_date(datetime(2026, 6, 5, 13, 40, tzinfo=TAIPEI_TZ)),
            date(2026, 6, 5),
        )
        self.assertEqual(
            _expected_latest_price_date(datetime(2026, 6, 6, 10, 0, tzinfo=TAIPEI_TZ)),
            date(2026, 6, 5),
        )

    def test_fresh_price_data_and_cumulative_series(self):
        df = pd.DataFrame({"Close": [1, 2]}, index=pd.to_datetime(["2026-06-04", "2026-06-05"]))

        self.assertTrue(_is_fresh_price_data(df, date(2026, 6, 5)))
        self.assertFalse(_is_fresh_price_data(df, date(2026, 6, 6)))
        self.assertEqual(_cumulative([10, -3, "bad", 2]), [10.0, 7.0, 9.0])


if __name__ == "__main__":
    unittest.main()
