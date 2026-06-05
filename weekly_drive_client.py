"""Low-level Google Drive client and file upload helpers."""

import os
from pathlib import Path

from weekly_logging import log, warn


def build_google_drive_credentials():
    scopes = ["https://www.googleapis.com/auth/drive"]
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if refresh_token and client_id and client_secret:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            credentials = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=scopes,
            )
            credentials.refresh(Request())
            return credentials, "OAuth"
        except Exception as exc:
            msg = str(exc)
            if "invalid_grant" in msg or "expired" in msg.lower() or "revoked" in msg.lower():
                warn("Google OAuth refresh token 已失效或被撤銷，請重新授權並更新 GitHub secret GOOGLE_OAUTH_REFRESH_TOKEN")
            warn(f"Google OAuth 憑證失敗：{exc}")

    return None, ""


def build_google_drive_service():
    try:
        from googleapiclient.discovery import build
    except Exception as exc:
        warn(f"未安裝 Google Drive API 套件：{exc}")
        return None, ""

    credentials, auth_mode = build_google_drive_credentials()
    if not credentials:
        return None, ""
    return build("drive", "v3", credentials=credentials, cache_discovery=False), auth_mode


def drive_name_query(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def upload_file_to_drive(file_path: Path, folder_id: str, mime_type: str,
                         file_name: str | None = None, make_public: bool = False,
                         file_id: str | None = None) -> dict | None:
    try:
        from googleapiclient.http import MediaFileUpload
    except Exception as exc:
        warn(f"未安裝 Google Drive API 套件，跳過上傳：{exc}")
        return None

    service, auth_mode = build_google_drive_service()
    if not service:
        warn("未設定 Google OAuth 憑證，已保留本機 PDF 但跳過上傳")
        return None
    name = file_name or file_path.name
    try:
        log(f"使用 Google Drive {auth_mode} 憑證上傳 PDF")
        media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=False)
        target = None
        if file_id:
            try:
                target = service.files().get(
                    fileId=file_id,
                    fields="id,name,webViewLink",
                    supportsAllDrives=True,
                ).execute()
            except Exception as exc:
                warn(f"固定 file_id 無法讀取，改用檔名搜尋：{exc}")
        if not target:
            query = (
                f"'{folder_id}' in parents and "
                f"name = '{drive_name_query(name)}' and "
                "trashed = false"
            )
            existing = service.files().list(
                q=query,
                fields="files(id,name,webViewLink)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute().get("files", [])
            target = existing[0] if existing else None

        if target:
            uploaded = service.files().update(
                fileId=target["id"],
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()
            log(f"已更新 Google Drive PDF：{uploaded.get('name')}｜file_id={uploaded.get('id')}")
        else:
            uploaded = service.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            ).execute()
            log(f"已建立 Google Drive PDF：{uploaded.get('name')}｜file_id={uploaded.get('id')}")

        if make_public:
            try:
                service.permissions().create(
                    fileId=uploaded["id"],
                    body={"type": "anyone", "role": "reader"},
                    supportsAllDrives=True,
                ).execute()
            except Exception as exc:
                warn(f"設定公開讀取失敗，請確認 Drive 權限：{exc}")
        return uploaded
    except Exception as exc:
        warn(f"上傳 Google Drive PDF 失敗：{exc}")
        return None
