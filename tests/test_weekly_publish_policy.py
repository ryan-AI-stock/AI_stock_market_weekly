import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from weekly_publish_policy import email_disabled, handle_drive_publish_failure, is_github_actions


class PublishPolicyTests(unittest.TestCase):
    def test_email_disabled_defaults_to_false_when_email_config_missing(self):
        self.assertFalse(email_disabled({}))
        self.assertFalse(email_disabled({"email": {"enabled": True}}))
        self.assertTrue(email_disabled({"email": {"enabled": False}}))

    def test_drive_failure_raises_on_github_actions_when_email_is_disabled(self):
        console = StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            self.assertTrue(is_github_actions())
            with redirect_stdout(console):
                with self.assertRaises(RuntimeError):
                    handle_drive_publish_failure(
                        {"email": {"enabled": False}},
                        "Drive failed",
                    )

        self.assertIn("❌ Drive failed", console.getvalue())
        self.assertIn("GitHub Actions 將中止流程", console.getvalue())

    def test_drive_failure_does_not_raise_outside_github_actions(self):
        console = StringIO()
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_github_actions())
            with redirect_stdout(console):
                handle_drive_publish_failure(
                    {"email": {"enabled": False}},
                    "Drive failed",
                )

        self.assertIn("保留本機產出檔", console.getvalue())

    def test_drive_failure_does_not_raise_when_email_is_enabled(self):
        console = StringIO()
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            with redirect_stdout(console):
                handle_drive_publish_failure(
                    {"email": {"enabled": True}},
                    "Drive failed",
                )

        self.assertIn("保留本機產出檔", console.getvalue())


if __name__ == "__main__":
    unittest.main()
