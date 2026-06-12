import unittest

import pandas as pd

from stock_market_tracking_system import evaluate_weighted, institutional_price_confirmation
from weekly_indicators import calc_indicators


def _scfg() -> dict:
    return {
        "ma_periods": {"short": 10, "mid": 20, "long": 60},
        "thresholds": {
            "kd_buy": 30,
            "kd_sell": 70,
            "vol_ma_period": 10,
            "obv_ma_period": 10,
            "bias60_p_low": 5,
            "bias60_p_high": 95,
        },
        "pyramid": {},
        "use_obv": False,
        "use_vol_trend": False,
        "use_institutional": True,
        "use_fx": False,
        "use_rates": False,
        "bias60_locked": False,
    }


def _indicator_frame(closes: list[float]) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-01", periods=len(closes))
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": [value + 1 for value in closes],
            "Low": [value - 1 for value in closes],
            "Close": closes,
            "Volume": [1000.0] * len(closes),
        },
        index=index,
    )
    return calc_indicators(frame, _scfg())


def _strong_sell_inst() -> dict:
    return {
        "success": True,
        "date": "2026-06-11",
        "foreign_net": -60.0,
        "invest_net": -60.0,
        "dealer_net": 0.0,
        "total_net": -120.0,
    }


class WeeklyInstitutionalConfirmationTests(unittest.TestCase):
    def test_price_confirmation_flags_10_day_drawdown(self):
        frame = _indicator_frame(list(range(100, 170)) + [170, 172, 174, 176, 178, 180, 178, 174, 170, 164])
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]

        result = institutional_price_confirmation(
            frame,
            float(latest["Close"]),
            float(latest["MA10"]),
            float(latest["MA20"]),
            float(latest["MACD_hist"]),
            float(previous["MACD_hist"]),
        )

        self.assertTrue(result["confirmed"])
        self.assertLessEqual(result["drawdown10_pct"], -8.0)
        self.assertIn("價格確認", result["note"])

    def test_unconfirmed_institutional_sell_is_downweighted(self):
        frame = _indicator_frame(list(range(100, 180)))

        result = evaluate_weighted(frame, _scfg(), inst=_strong_sell_inst(), macro={}, inst_week={})

        inst_item = next(item for item in result["items"] if item[0] == "三大法人")
        self.assertIn("價格未確認", inst_item[1])
        self.assertIn("風險條件+8", inst_item[3])

    def test_confirmed_institutional_sell_keeps_full_weight(self):
        frame = _indicator_frame(list(range(100, 170)) + [170, 172, 174, 176, 178, 180, 178, 174, 170, 164])

        result = evaluate_weighted(frame, _scfg(), inst=_strong_sell_inst(), macro={}, inst_week={})

        inst_item = next(item for item in result["items"] if item[0] == "三大法人")
        self.assertIn("價格確認", inst_item[3])
        self.assertIn("風險條件+15", inst_item[3])


if __name__ == "__main__":
    unittest.main()
