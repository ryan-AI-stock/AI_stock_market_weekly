import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stock_market_tracking_system import (
    TAIPEI_TZ,
    analyze_weekly_watchlist,
    prepare_weekly_run,
    publish_weekly_report_outputs,
)


class WeeklyOrchestrationTests(unittest.TestCase):
    def test_prepare_weekly_run_builds_report_context(self):
        cfg = {"watchlist": []}
        now = datetime(2026, 6, 5, 16, 0, tzinfo=TAIPEI_TZ)
        with patch(
            "stock_market_tracking_system.resolve_report_target",
            return_value=date(2026, 6, 5),
        ):
            run = prepare_weekly_run(cfg, now_tw=now, force_run=True)

        self.assertTrue(run["force_run"])
        self.assertEqual(run["expected_date"], "2026-06-05")
        self.assertEqual(run["today"], "2026-06-05")
        self.assertEqual(run["backup_pdf_name"], "每週台股報告_20260605.pdf")

    def test_analyze_weekly_watchlist_keeps_weighted_evaluator_entrypoint(self):
        cfg = {
            "lookback_days": 120,
            "thresholds": {},
            "ma_periods": {},
            "watchlist": [{"name": "台積電", "ticker": "2330.TW", "note": "觀察"}],
        }
        run = {
            "target_date": date(2026, 6, 5),
            "report_meta": {"week": 23},
        }
        market_inputs = {"macro": {"success": True}, "market_inst_value_week": {"success": False}}
        df = pd.DataFrame(
            {"Open": [1], "High": [2], "Low": [1], "Close": [2], "Volume": [100]},
            index=pd.to_datetime(["2026-06-05"]),
        )
        evaluated = {
            "emoji": "✅",
            "summary": "測試",
            "effective_buy": 50,
            "effective_sell": 10,
            "buy_score": 50,
            "sell_score": 10,
            "b60": {"bias60": 1.2},
            "weekly": {},
        }

        with (
            patch("stock_market_tracking_system.fetch_data", return_value=df),
            patch("stock_market_tracking_system.calc_indicators", side_effect=lambda frame, _cfg: frame),
            patch("stock_market_tracking_system.fetch_institutional", return_value={"success": True}),
            patch("stock_market_tracking_system.fetch_weekly_institutional", return_value={"success": True}),
            patch("stock_market_tracking_system.evaluate_weighted", return_value=evaluated) as evaluate,
        ):
            results = analyze_weekly_watchlist(cfg, run, market_inputs)

        self.assertEqual(results[0][0], "台積電")
        self.assertEqual(results[0][2]["data_date"], "2026-06-05")
        self.assertEqual(results[0][2]["stock_note"], "觀察")
        evaluate.assert_called_once()

    def test_publish_weekly_report_outputs_returns_public_and_backup_links(self):
        cfg = {"email": {"enabled": False}, "public_report": {"enabled": True}, "drive_report": {"enabled": True}}
        run = {
            "today": "2026-06-05",
            "report_meta": {"date": "2026-06-05"},
            "backup_pdf_name": "每週台股報告_20260605.pdf",
        }
        market_inputs = {"macro": {}, "news_items": []}

        with (
            patch("stock_market_tracking_system.build_email_html", return_value="<html>email</html>"),
            patch("stock_market_tracking_system.save_email_preview", return_value=Path("email_preview.html")),
            patch("stock_market_tracking_system.build_public_report_html", return_value="<html>public</html>"),
            patch("stock_market_tracking_system.save_public_report_file", return_value=Path("public.pdf")),
            patch("stock_market_tracking_system.upload_public_report_file", return_value="https://drive/public"),
            patch("stock_market_tracking_system.render_report_pdf", return_value=Path("backup.pdf")),
            patch("stock_market_tracking_system.upload_report_file_to_drive", return_value="https://drive/backup"),
        ):
            links = publish_weekly_report_outputs(cfg, run, [], market_inputs, [])

        self.assertEqual(
            links,
            {"public_link": "https://drive/public", "backup_link": "https://drive/backup"},
        )


if __name__ == "__main__":
    unittest.main()
