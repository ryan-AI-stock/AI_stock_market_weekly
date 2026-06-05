import unittest

import pandas as pd

from weekly_indicators import calc_indicators, calc_pyramid, eval_bias60


def _indicator_cfg() -> dict:
    return {
        "ma_periods": {"short": 5, "mid": 20, "long": 60},
        "thresholds": {
            "vol_ma_period": 5,
            "obv_ma_period": 5,
            "bias60_p_low": 5,
            "bias60_p_high": 95,
        },
        "pyramid": {"add_per_drop_pct": 5.0, "time_rebalance_days": 20},
    }


class WeeklyIndicatorTests(unittest.TestCase):
    def test_calc_indicators_adds_expected_columns_and_bias_attrs(self):
        closes = [100 + i for i in range(80)]
        df = pd.DataFrame(
            {
                "Open": closes,
                "High": [value + 1 for value in closes],
                "Low": [value - 1 for value in closes],
                "Close": closes,
                "Volume": [1000 + i * 10 for i in range(80)],
            }
        )

        result = calc_indicators(df, _indicator_cfg())

        for column in ("MA5", "MA20", "MA60", "BIAS60", "BIAS60_Z", "K", "D", "MACD_hist", "Vol_MA", "OBV", "OBV_MA"):
            self.assertIn(column, result.columns)
        for attr in ("bias60_p_high", "bias60_p_low", "bias60_mean", "bias60_std"):
            self.assertIn(attr, result.attrs)

        bias = eval_bias60(result, _indicator_cfg())
        self.assertIn(bias["zone"], {"overheated", "normal", "oversold"})
        self.assertIn("note", bias)

    def test_calc_pyramid_returns_observation_suggestions_for_buy_signal(self):
        df = pd.DataFrame({"Close": [100] * 19 + [98]})
        result = calc_pyramid(df, _indicator_cfg(), "BUY_STRONG")

        self.assertLess(result["drop_pct"], 0)
        self.assertTrue(result["suggestions"])


if __name__ == "__main__":
    unittest.main()
