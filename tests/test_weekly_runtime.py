import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from weekly_runtime import env_flag, load_config_file, write_github_output


class RuntimeConfigTests(unittest.TestCase):
    def test_load_config_file_reads_explicit_json_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text('{"watchlist": ["2330.TW"]}', encoding="utf-8")

            self.assertEqual(
                load_config_file(config_path),
                {"watchlist": ["2330.TW"]},
            )

    def test_env_flag_uses_existing_force_run_values(self):
        for value in ("1", "true", "TRUE", "yes", "y"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"FORCE_RUN_REPORT": value}):
                    self.assertTrue(env_flag("FORCE_RUN_REPORT"))

    def test_env_flag_defaults_false_and_rejects_other_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(env_flag("FORCE_RUN_REPORT"))
            self.assertTrue(env_flag("FORCE_RUN_REPORT", default=True))
        with patch.dict(os.environ, {"FORCE_RUN_REPORT": "false"}):
            self.assertFalse(env_flag("FORCE_RUN_REPORT"))


class GithubOutputTests(unittest.TestCase):
    def test_write_github_output_appends_same_console_and_file_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "github_output.txt"
            console = StringIO()

            with redirect_stdout(console):
                write_github_output("should_run", "true", output_path)

            self.assertEqual(output_path.read_text(encoding="utf-8"), "should_run=true\n")
            self.assertEqual(console.getvalue(), "should_run=true\n")


if __name__ == "__main__":
    unittest.main()
