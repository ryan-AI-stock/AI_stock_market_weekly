import unittest
from contextlib import redirect_stdout
from io import StringIO

from weekly_logging import error, log, success, warn


class WeeklyLoggingTests(unittest.TestCase):
    def test_log_helpers_keep_existing_console_prefixes(self):
        console = StringIO()
        with redirect_stdout(console):
            log("plain")
            warn("warning")
            error("error")
            success("success")

        self.assertEqual(
            console.getvalue(),
            "plain\n⚠️  warning\n❌ error\n✅ success\n",
        )


if __name__ == "__main__":
    unittest.main()
