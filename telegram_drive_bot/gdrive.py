import io
import json
import os
import re
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


def safe_name(name: str, limit: int = 140) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name or "Untitled")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:limit] or "Untitled"


def load_json_env(name: str, file_name_env: str) -> dict:
    raw = os.getenv(name, "").strip()
    if raw:
        return json.loads(raw)

    file_path = os.getenv(file_name_env, "").strip()
    if file_path:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    raise RuntimeError(f"Set {name} or {file_name_env}")


def drive_service():
    token_info = load_json_env("GOOGLE_OAUTH_TOKEN_JSON", "GOOGLE_OAUTH_TOKEN_FILE")
    creds = Credentials.from_authorized_user_info(token_info, SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(service, name: str, parent_id: Optional[str] = None) -> str:
    folder_name = safe_name(name)
    escaped = folder_name.replace("'", "\\'")
    query = "mimeType='application/vnd.google-apps.folder' and trashed=false and name='{}'".format(escaped)
    if parent_id:
        query += " and '{}' in parents".format(parent_id)

    result = service.files().list(
        q=query,
        fields="files(id, name)",
        pageSize=1,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = result.get("files", [])
    if files:
        return files[0]["id"]

    body = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]

    created = service.files().create(body=body, fields="id", supportsAllDrives=True).execute()
    return created["id"]


def upload_file(file_path: str, title: str = "yt-dlp uploads", mime_type: str = "video/mp4") -> str:
    service = drive_service()
    root_id = os.getenv("GDRIVE_ROOT_FOLDER_ID", "").strip() or None

    folder_id = root_id
    if os.getenv("GDRIVE_CATEGORY_BY_TITLE", "0").strip() == "1":
        folder_id = find_or_create_folder(service, title, root_id)

    metadata = {"name": safe_name(os.path.basename(file_path), 180)}
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink, webContentLink",
        supportsAllDrives=True,
    ).execute()

    file_id = created["id"]
    if os.getenv("GDRIVE_MAKE_PUBLIC", "1").strip() != "0":
        service.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()

    return created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
