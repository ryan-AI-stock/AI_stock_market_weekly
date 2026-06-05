"""Google Drive publish target resolution for weekly reports."""

import os


def _env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def _first_value(*values: object) -> str:
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def get_acceptance_drive_folder_id() -> str:
    return _env_value("REPORT_TEST_DRIVE_FOLDER_ID")


def in_acceptance_drive_mode() -> bool:
    return bool(get_acceptance_drive_folder_id())


def resolve_backup_drive_folder_id(drive_cfg: dict) -> str:
    return _first_value(
        get_acceptance_drive_folder_id(),
        _env_value("WEEKLY_REPORT_DRIVE_FOLDER_ID"),
        _env_value("GOOGLE_DRIVE_FOLDER_ID"),
        drive_cfg.get("folder_id"),
    )


def resolve_public_report_folder_id(public_cfg: dict) -> str:
    return _first_value(
        get_acceptance_drive_folder_id(),
        _env_value("PUBLIC_REPORT_DRIVE_FOLDER_ID"),
        _env_value("WEEKLY_PUBLIC_REPORT_DRIVE_FOLDER_ID"),
        public_cfg.get("folder_id"),
    )


def resolve_public_report_file_id(public_cfg: dict) -> str:
    if in_acceptance_drive_mode():
        return ""
    return _first_value(
        _env_value("PUBLIC_REPORT_DRIVE_FILE_ID"),
        public_cfg.get("fixed_file_id"),
    )
