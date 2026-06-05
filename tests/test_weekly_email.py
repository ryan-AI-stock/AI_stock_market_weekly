import os
import unittest
from email import message_from_string
from email.header import decode_header, make_header
from unittest.mock import Mock, patch

from weekly_email import send_report_email


REPORT_META = {"week": 23, "week_key": "2026-W23"}


class WeeklyEmailTests(unittest.TestCase):
    def test_send_report_email_skips_when_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                send_report_email(
                    {"email": {"enabled": False}},
                    "<html></html>",
                    "2026-06-05",
                    REPORT_META,
                )
            )

    def test_send_report_email_skips_when_secrets_are_missing(self):
        cfg = {"email": {"enabled": True, "subject": "週報 {date} W{week}"}}
        with patch.dict(os.environ, {"SMTP_USERNAME": "user@example.com"}, clear=True):
            with patch("weekly_email.smtplib.SMTP_SSL") as smtp:
                self.assertFalse(send_report_email(cfg, "<html></html>", "2026-06-05", REPORT_META))

        smtp.assert_not_called()

    def test_send_report_email_uses_config_subject_and_recipients(self):
        cfg = {
            "email": {
                "enabled": True,
                "subject": "週報 {date} 第{week}週 {week_key}",
            }
        }
        smtp_instance = Mock()
        with (
            patch.dict(
                os.environ,
                {
                    "SMTP_USERNAME": "sender@example.com",
                    "SMTP_PASSWORD": "password",
                    "REPORT_EMAIL_TO": "a@example.com, b@example.com",
                },
                clear=True,
            ),
            patch("weekly_email.smtplib.SMTP_SSL", return_value=smtp_instance),
        ):
            self.assertTrue(send_report_email(cfg, "<html>body</html>", "2026-06-05", REPORT_META))

        smtp_instance.login.assert_called_once_with("sender@example.com", "password")
        smtp_instance.sendmail.assert_called_once()
        args = smtp_instance.sendmail.call_args.args
        self.assertEqual(args[0], "sender@example.com")
        self.assertEqual(args[1], ["a@example.com", "b@example.com"])
        message = message_from_string(args[2])
        subject = str(make_header(decode_header(message["Subject"])))
        self.assertIn("2026-W23", subject)
        smtp_instance.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
