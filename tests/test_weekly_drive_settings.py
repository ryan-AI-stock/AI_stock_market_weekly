import os
import unittest
from unittest.mock import patch

from weekly_drive_settings import (
    get_acceptance_drive_folder_id,
    in_acceptance_drive_mode,
    resolve_backup_drive_folder_id,
    resolve_public_report_file_id,
    resolve_public_report_folder_id,
)


TEST_DRIVE_FOLDER_ID = "1Qx6D7UG39JI3w7HPPQGEddoDeV9lotGz"


class DriveSettingsTests(unittest.TestCase):
    def test_acceptance_folder_takes_priority_for_backup_and_public_targets(self):
        with patch.dict(
            os.environ,
            {
                "REPORT_TEST_DRIVE_FOLDER_ID": TEST_DRIVE_FOLDER_ID,
                "WEEKLY_REPORT_DRIVE_FOLDER_ID": "weekly-backup-folder",
                "PUBLIC_REPORT_DRIVE_FOLDER_ID": "public-folder",
                "PUBLIC_REPORT_DRIVE_FILE_ID": "production-fixed-file-id",
            },
            clear=True,
        ):
            self.assertEqual(get_acceptance_drive_folder_id(), TEST_DRIVE_FOLDER_ID)
            self.assertTrue(in_acceptance_drive_mode())
            self.assertEqual(
                resolve_backup_drive_folder_id({"folder_id": "config-backup-folder"}),
                TEST_DRIVE_FOLDER_ID,
            )
            self.assertEqual(
                resolve_public_report_folder_id({"folder_id": "config-public-folder"}),
                TEST_DRIVE_FOLDER_ID,
            )
            self.assertEqual(
                resolve_public_report_file_id({"fixed_file_id": "config-fixed-file-id"}),
                "",
            )

    def test_production_public_file_id_uses_env_before_config(self):
        with patch.dict(
            os.environ,
            {
                "PUBLIC_REPORT_DRIVE_FOLDER_ID": "public-folder",
                "PUBLIC_REPORT_DRIVE_FILE_ID": "env-fixed-file-id",
            },
            clear=True,
        ):
            self.assertFalse(in_acceptance_drive_mode())
            self.assertEqual(
                resolve_public_report_folder_id({"folder_id": "config-public-folder"}),
                "public-folder",
            )
            self.assertEqual(
                resolve_public_report_file_id({"fixed_file_id": "config-fixed-file-id"}),
                "env-fixed-file-id",
            )

    def test_config_values_are_used_when_env_is_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_backup_drive_folder_id({"folder_id": "config-backup-folder"}),
                "config-backup-folder",
            )
            self.assertEqual(
                resolve_public_report_folder_id({"folder_id": "config-public-folder"}),
                "config-public-folder",
            )
            self.assertEqual(
                resolve_public_report_file_id({"fixed_file_id": "config-fixed-file-id"}),
                "config-fixed-file-id",
            )


if __name__ == "__main__":
    unittest.main()
