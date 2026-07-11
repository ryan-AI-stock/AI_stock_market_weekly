import os
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from stock_market_tracking_system import (
    build_public_report_html,
    build_trade_plan,
    get_drive_target_folder_id,
    public_report_is_complete,
    run_schedule_gate,
    upload_public_report_file,
    upload_report_file_to_drive,
)


TEST_DRIVE_FOLDER_ID = "1Qx6D7UG39JI3w7HPPQGEddoDeV9lotGz"


def _minimal_result(close: float) -> dict:
    trade_plan = build_trade_plan(
        "BUY_STRONG",
        {"key": "STRONG_BULL", "label": "大多頭", "color": "#000"},
        {"zone": "normal"},
    )
    return {
        "weekly": {
            "posture": "趨勢條件仍成立",
            "posture_color": "#000",
            "week_chg_pct": 1.5,
            "trend_summary": "趨勢條件仍成立。",
            "next_focus": "觀察關鍵均線與量能。",
            "week_range_label": "2026-06-01 - 2026-06-05",
            "institutional_daily_values": [],
        },
        "close": close,
        "border": "#000",
        "trade_plan": trade_plan,
        "effective_buy": 50,
        "effective_sell": 10,
        "items": [],
    }


class PublicReportContractTests(unittest.TestCase):
    def test_public_report_keeps_four_page_shell_and_key_sections(self):
        html = build_public_report_html(
            [
                ("台灣加權指數", "^TWII", _minimal_result(23000)),
                ("台積電", "2330.TW", _minimal_result(1000)),
            ],
            "2026-06-05",
            cfg={},
            macro={},
            news_items=[],
            event_items=[],
        )

        self.assertEqual(html.count("class='page'"), 4)
        self.assertIn("@media screen", html)
        self.assertIn("max-width:900px", html)
        self.assertEqual(html.count("class='note hero-note'"), 1)
        self.assertNotIn("class='summary'", html)
        self.assertNotIn("class='status'>趨勢條件仍成立</div><div class='note'", html)
        self.assertIn("<b>趨勢條件仍成立。</b>觀察關鍵均線與量能。｜週報偏向", html)
        self.assertIn("週報偏向中大型權值股與中長線條件觀察", html)
        for text in (
            "每週台股報告",
            "權值股總覽",
            "市場與強勢標的雷達",
            "修正與觀察標的雷達",
            "免費摘要版",
            "僅供參考，不構成投資建議",
            "正向條件50 / 風險條件10",
        ):
            self.assertIn(text, html)


class SignalEvaluatorContractTests(unittest.TestCase):
    def test_legacy_unweighted_evaluate_is_removed(self):
        source_path = Path(__file__).resolve().parents[1] / "stock_market_tracking_system.py"
        source = source_path.read_text(encoding="utf-8")

        self.assertNotIn("def evaluate(df:", source)
        self.assertIn("def evaluate_weighted(", source)
        self.assertIn("r    = evaluate_weighted(df, scfg, inst, macro, inst_week)", source)


class DrivePublishContractTests(unittest.TestCase):
    def test_acceptance_folder_overrides_backup_production_folder(self):
        cfg = {
            "drive_report": {
                "enabled": True,
                "folder_id": "production-backup-folder",
                "folder_path": [],
            }
        }
        with patch.dict(
            os.environ,
            {
                "REPORT_TEST_DRIVE_FOLDER_ID": TEST_DRIVE_FOLDER_ID,
                "WEEKLY_REPORT_DRIVE_FOLDER_ID": "production-backup-folder",
            },
        ):
            folder_id = get_drive_target_folder_id(object(), cfg, {}, create=False)

        self.assertEqual(folder_id, TEST_DRIVE_FOLDER_ID)

    def test_public_pdf_upload_uses_test_folder_and_fixed_file_semantics(self):
        cfg = {
            "public_report": {
                "enabled": True,
                "folder_id": "production-folder",
                "fixed_file_name": "每週台股報告.pdf",
                "make_public": True,
            }
        }
        with patch.dict(
            os.environ,
            {
                "PUBLIC_REPORT_DRIVE_FOLDER_ID": TEST_DRIVE_FOLDER_ID,
                "PUBLIC_REPORT_DRIVE_FILE_ID": "test-fixed-file-id",
            },
        ):
            with patch(
                "stock_market_tracking_system.upload_file_to_drive",
                return_value={"webViewLink": "https://example.test/fixed"},
            ) as upload:
                link = upload_public_report_file(Path("dummy.pdf"), cfg)

        self.assertEqual(link, "https://example.test/fixed")
        upload.assert_called_once_with(
            Path("dummy.pdf"),
            TEST_DRIVE_FOLDER_ID,
            "application/pdf",
            file_name="每週台股報告.pdf",
            make_public=True,
            file_id="test-fixed-file-id",
        )

    def test_acceptance_folder_ignores_production_fixed_file_id(self):
        cfg = {
            "public_report": {
                "enabled": True,
                "folder_id": "production-folder",
                "fixed_file_id": "production-fixed-file-id",
                "fixed_file_name": "每週台股報告.pdf",
                "make_public": True,
            }
        }
        with patch.dict(
            os.environ,
            {
                "REPORT_TEST_DRIVE_FOLDER_ID": TEST_DRIVE_FOLDER_ID,
                "PUBLIC_REPORT_DRIVE_FOLDER_ID": "production-public-folder",
                "PUBLIC_REPORT_DRIVE_FILE_ID": "production-fixed-file-id",
            },
        ):
            with patch(
                "stock_market_tracking_system.upload_file_to_drive",
                return_value={"webViewLink": "https://example.test/acceptance"},
            ) as upload:
                link = upload_public_report_file(Path("dummy.pdf"), cfg)

        self.assertEqual(link, "https://example.test/acceptance")
        upload.assert_called_once_with(
            Path("dummy.pdf"),
            TEST_DRIVE_FOLDER_ID,
            "application/pdf",
            file_name="每週台股報告.pdf",
            make_public=True,
            file_id=None,
        )

    def test_backup_pdf_upload_uses_resolved_folder_and_report_name(self):
        cfg = {"drive_report": {"enabled": True}}
        report_path = Path("每週台股報告_20260605.pdf")
        with (
            patch(
                "stock_market_tracking_system.build_google_drive_service",
                return_value=(object(), "test"),
            ),
            patch(
                "stock_market_tracking_system.get_drive_target_folder_id",
                return_value=TEST_DRIVE_FOLDER_ID,
            ),
            patch(
                "stock_market_tracking_system.upload_file_to_drive",
                return_value={"webViewLink": "https://example.test/backup"},
            ) as upload,
        ):
            link = upload_report_file_to_drive(
                report_path,
                "2026-06-05",
                cfg,
                file_name=report_path.name,
            )

        self.assertEqual(link, "https://example.test/backup")
        upload.assert_called_once_with(
            report_path,
            TEST_DRIVE_FOLDER_ID,
            "application/pdf",
            file_name=report_path.name,
        )

    def test_public_report_completion_uses_fixed_pdf_modified_time(self):
        cfg = {"public_report": {"enabled": True, "fixed_file_id": "fixed-weekly-pdf"}}
        service = Mock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "fixed-weekly-pdf",
            "name": "每週台股報告.pdf",
            "modifiedTime": "2026-06-05T08:10:00Z",
            "trashed": False,
        }

        with patch("stock_market_tracking_system.build_google_drive_service", return_value=(service, "test")):
            self.assertTrue(public_report_is_complete(date(2026, 6, 5), cfg))

    def test_public_report_completion_rejects_stale_fixed_pdf(self):
        cfg = {"public_report": {"enabled": True, "fixed_file_id": "fixed-weekly-pdf"}}
        service = Mock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "fixed-weekly-pdf",
            "name": "每週台股報告.pdf",
            "modifiedTime": "2026-06-05T06:59:00Z",
            "trashed": False,
        }

        with patch("stock_market_tracking_system.build_google_drive_service", return_value=(service, "test")):
            self.assertFalse(public_report_is_complete(date(2026, 6, 5), cfg))


class ScheduleGateContractTests(unittest.TestCase):
    def test_schedule_gate_stops_when_public_report_is_current(self):
        writes = []
        with (
            patch("stock_market_tracking_system.load_config", return_value={}),
            patch(
                "stock_market_tracking_system.resolve_report_target",
                return_value=date(2026, 6, 5),
            ),
            patch("stock_market_tracking_system.public_report_is_complete", return_value=True),
            patch(
                "stock_market_tracking_system._write_github_output",
                side_effect=lambda name, value: writes.append((name, value)),
            ),
        ):
            run_schedule_gate()

        self.assertIn(("target_date", "2026-06-05"), writes)
        self.assertIn(("should_run", "false"), writes)

    def test_schedule_gate_runs_when_public_report_is_not_current(self):
        writes = []
        with (
            patch("stock_market_tracking_system.load_config", return_value={}),
            patch(
                "stock_market_tracking_system.resolve_report_target",
                return_value=date(2026, 6, 5),
            ),
            patch("stock_market_tracking_system.public_report_is_complete", return_value=False),
            patch(
                "stock_market_tracking_system._write_github_output",
                side_effect=lambda name, value: writes.append((name, value)),
            ),
        ):
            run_schedule_gate()

        self.assertIn(("target_date", "2026-06-05"), writes)
        self.assertIn(("should_run", "true"), writes)

    def test_schedule_gate_force_run_bypasses_existing_backup_check(self):
        writes = []
        with (
            patch.dict(os.environ, {"FORCE_RUN_REPORT": "true"}, clear=False),
            patch("stock_market_tracking_system.load_config", return_value={}),
            patch(
                "stock_market_tracking_system.resolve_report_target",
                return_value=date(2026, 6, 5),
            ),
            patch("stock_market_tracking_system.public_report_is_complete") as complete,
            patch(
                "stock_market_tracking_system._write_github_output",
                side_effect=lambda name, value: writes.append((name, value)),
            ),
        ):
            run_schedule_gate()

        self.assertIn(("target_date", "2026-06-05"), writes)
        self.assertIn(("should_run", "true"), writes)
        complete.assert_not_called()

    def test_schedule_gate_manual_rerun_bypasses_fixed_pdf_completion_gate(self):
        writes = []
        with (
            patch.dict(os.environ, {"MANUAL_RERUN": "true", "FORCE_RUN_REPORT": "false"}, clear=False),
            patch("stock_market_tracking_system.load_config", return_value={}),
            patch(
                "stock_market_tracking_system.resolve_report_target",
                return_value=date(2026, 6, 19),
            ) as resolve_target,
            patch("stock_market_tracking_system.public_report_is_complete") as complete,
            patch(
                "stock_market_tracking_system._write_github_output",
                side_effect=lambda name, value: writes.append((name, value)),
            ),
        ):
            run_schedule_gate()

        resolve_target.assert_called_once()
        self.assertTrue(resolve_target.call_args.args[1])
        complete.assert_not_called()
        self.assertIn(("manual_rerun", "true"), writes)
        self.assertIn(("actual_report_date", "2026-06-19"), writes)
        self.assertIn(("fallback_reason", "manual_rerun_latest_complete_week"), writes)
        self.assertIn(("target_date", "2026-06-19"), writes)
        self.assertIn(("should_run", "true"), writes)

    def test_schedule_gate_stops_without_failure_when_calendar_is_temporarily_unavailable(self):
        writes = []
        with (
            patch("stock_market_tracking_system.load_config", return_value={}),
            patch(
                "stock_market_tracking_system.resolve_report_target",
                side_effect=RuntimeError("TWSE calendar unavailable"),
            ),
            patch(
                "stock_market_tracking_system._write_github_output",
                side_effect=lambda name, value: writes.append((name, value)),
            ),
        ):
            run_schedule_gate()

        self.assertIn(("target_date", ""), writes)
        self.assertIn(("should_run", "false"), writes)


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_keeps_hourly_schedule_and_schedule_gate(self):
        workflow_path = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "weekly_run.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertIn("- cron: '0 * * * *'", workflow)
        self.assertIn(
            "run: python stock_market_tracking_system.py --schedule-gate",
            workflow,
        )
        self.assertIn("repository: ryan-AI-stock/AI_stock_schedule_rules", workflow)
        self.assertIn("path: AI_stock_schedule_rules", workflow)
        self.assertIn("fixed public Drive PDF is current", workflow)
        self.assertNotIn("Drive backup PDF exists", workflow)
        self.assertEqual(
            workflow.count("if: steps.schedule-gate.outputs.should_run == 'true'"),
            2,
        )
        self.assertIn("test_drive_folder_id:", workflow)
        self.assertIn("default: 'true'", workflow)
        self.assertIn("MANUAL_RERUN:", workflow)
        self.assertEqual(workflow.count("REPORT_TEST_DRIVE_FOLDER_ID:"), 2)
        self.assertEqual(workflow.count("MANUAL_RERUN:"), 2)
        self.assertEqual(workflow.count("SCHEDULE_RULES_PATH:"), 2)
        self.assertIn('if [ "$status" -eq 75 ]', workflow)
        self.assertIn("下一次每小時排程會自動重試", workflow)
        self.assertIn("group: weekly-taiwan-stock-market-report", workflow)


if __name__ == "__main__":
    unittest.main()
