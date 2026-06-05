import unittest

from stock_market_tracking_system import (
    _social_score_impact,
    build_public_report_html,
    build_trade_plan,
    classify_weekly_posture,
    scoring_rules_html,
)


class ReportLanguageTests(unittest.TestCase):
    def setUp(self):
        self.strong_bull = {"key": "STRONG_BULL", "label": "大多頭", "color": "#000"}
        self.bear = {"key": "BEAR", "label": "空頭", "color": "#000"}
        self.normal_bias = {"zone": "normal"}

    def test_positive_plan_keeps_score_but_removes_position_instruction(self):
        plan = build_trade_plan("BUY_STRONG", self.strong_bull, self.normal_bias)

        self.assertEqual(plan["trade_pct"], 50)
        self.assertEqual(plan["headline"], "正向條件通過 5/10")
        self.assertNotIn("買進或加碼", plan["headline"])
        self.assertNotIn("%", plan["headline"])

    def test_risk_plan_keeps_score_but_removes_position_instruction(self):
        plan = build_trade_plan("SELL_STRONG", self.bear, self.normal_bias)

        self.assertEqual(plan["trade_pct"], 50)
        self.assertEqual(plan["headline"], "風險條件通過 5/10")
        self.assertNotIn("賣出或減碼", plan["headline"])
        self.assertNotIn("%", plan["headline"])

    def test_overheated_plan_uses_risk_observation_language(self):
        plan = build_trade_plan(
            "OVERHEATED_WEAK",
            self.strong_bull,
            {"zone": "overheated"},
        )

        self.assertEqual(plan["headline"], "追價風險偏高")
        self.assertNotIn("禁止追買", plan["headline"])

    def test_weekly_posture_uses_condition_language(self):
        posture, _color, _note = classify_weekly_posture(
            self.strong_bull,
            self.normal_bias,
            2.0,
            120,
            115,
            110,
            100,
            60,
            20,
        )

        self.assertEqual(posture, "趨勢條件仍成立")

    def test_scoring_rules_and_social_parser_use_condition_terms(self):
        html = scoring_rules_html()

        self.assertIn("正向條件與風險條件分數", html)
        self.assertNotIn("買進與賣出分數", html)
        self.assertEqual(
            _social_score_impact("分數影響:正向條件+25/風險條件+10"),
            (25.0, 10.0),
        )

    def test_public_report_contains_no_action_or_position_instruction(self):
        plan = build_trade_plan("BUY_STRONG", self.strong_bull, self.normal_bias)
        weekly = {
            "posture": "趨勢條件仍成立",
            "posture_color": "#000",
            "week_chg_pct": 1.5,
            "trend_summary": "趨勢條件仍成立。",
            "next_focus": "觀察關鍵均線與量能。",
            "week_range_label": "2026-06-01 - 2026-06-05",
            "institutional_daily_values": [],
        }
        market_result = {
            "weekly": weekly,
            "close": 23000,
            "border": "#000",
            "trade_plan": plan,
            "effective_buy": 50,
            "effective_sell": 10,
            "items": [],
        }
        stock_result = {
            **market_result,
            "close": 1000,
        }

        html = build_public_report_html(
            [
                ("台灣加權指數", "^TWII", market_result),
                ("台積電", "2330.TW", stock_result),
            ],
            "2026-06-05",
            cfg={},
            macro={},
            news_items=[],
            event_items=[],
        )

        for phrase in (
            "買進或加碼",
            "賣出或減碼",
            "禁止追買",
            "強勢續抱",
            "買賣分數",
        ):
            self.assertNotIn(phrase, html)
        self.assertIn("正向條件通過 5/10", html)
        self.assertIn("正向條件50 / 風險條件10", html)


if __name__ == "__main__":
    unittest.main()
