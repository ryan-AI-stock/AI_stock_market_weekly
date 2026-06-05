"""Publish failure policy for weekly report delivery."""

import os


def is_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def email_disabled(cfg: dict) -> bool:
    return not cfg.get("email", {}).get("enabled", True)


def handle_drive_publish_failure(cfg: dict, msg: str) -> None:
    print(f"❌ {msg}")
    if is_github_actions() and email_disabled(cfg):
        print("❌ Google Drive 是目前正式發布渠道，發布失敗，GitHub Actions 將中止流程")
        raise RuntimeError(msg)
    print("⚠️  本機或 Email 未關閉情境：保留本機產出檔，略過 Google Drive 上傳")
