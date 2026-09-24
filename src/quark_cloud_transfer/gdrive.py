from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .errors import DriveError

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"


def escape_q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveClient:
    def __init__(
        self,
        oauth: Dict[str, Any],
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 60,
    ) -> None:
        self.oauth = oauth
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    def _token(self, *, force: bool = False) -> str:
        now = time.time()
        if not force and self._access_token and now < self._expires_at - 60:
            return self._access_token
        response = self.session.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": self.oauth["client_id"],
                "client_secret": self.oauth["client_secret"],
                "refresh_token": self.oauth["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=self.timeout,
        )
        if not response.ok:
            raise DriveError(
                f"Google OAuth token refresh failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DriveError("Google OAuth token endpoint returned non-JSON data") from exc
        token = payload.get("access_token")
        if not token:
            raise DriveError("Google OAuth token response did not include access_token")
        self._access_token = str(token)
        expires_in = int(payload.get("expires_in") or 3600)
        self._expires_at = now + expires_in
        return self._access_token

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        retry_auth: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        request_headers = dict(headers or {})
        request_headers["Authorization"] = f"Bearer {self._token()}"
        timeout = kwargs.pop("timeout", self.timeout)
        response = self.session.request(
            method,
            url,
            headers=request_headers,
            timeout=timeout,
            **kwargs,
        )
        if response.status_code == 401 and retry_auth:
            request_headers["Authorization"] = f"Bearer {self._token(force=True)}"
            response = self.session.request(
                method,
                url,
                headers=request_headers,
                timeout=timeout,
                **kwargs,
            )
        return response

    def list_named(
        self,
        parent_id: str,
        name: str,
        mime_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses = [
            f"'{escape_q(parent_id)}' in parents",
            f"name = '{escape_q(name)}'",
            "trashed = false",
        ]
        if mime_type:
            clauses.append(f"mimeType = '{escape_q(mime_type)}'")
        response = self._request(
            "GET",
            DRIVE_API + "/files",
            params={
                "q": " and ".join(clauses),
                "fields": "files(id,name,size,mimeType,parents,md5Checksum)",
                "pageSize": 100,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        if not response.ok:
            raise DriveError(f"Google Drive list failed with HTTP {response.status_code}")
        return list(response.json().get("files") or [])

    def create_folder(self, parent_id: str, name: str) -> str:
        response = self._request(
            "POST",
            DRIVE_API + "/files",
            params={"supportsAllDrives": "true", "fields": "id"},
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        if not response.ok:
            raise DriveError(
                f"Google Drive folder creation failed with HTTP {response.status_code}"
            )
        folder_id = response.json().get("id")
        if not folder_id:
            raise DriveError("Google Drive folder creation returned no id")
        return str(folder_id)

    def ensure_folder_path(self, path: str, *, root_id: str = "root") -> str:
        current = root_id
        for part in [item for item in re.split(r"[\\/]+", path.strip("/\\")) if item]:
            found = self.list_named(current, part, FOLDER_MIME)
            if found:
                current = str(found[0]["id"])
            else:
                current = self.create_folder(current, part)
        return current

    def choose_destination(
        self,
        parent_id: str,
        name: str,
        source_size: int,
        *,
        on_exists: str,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        existing = [
            item
            for item in self.list_named(parent_id, name)
            if item.get("mimeType") != FOLDER_MIME
        ]
        if not existing:
            return name, None
        for item in existing:
            if str(item.get("size", "")) == str(source_size):
                return None, item
        if on_exists == "skip":
            raise DriveError(
                f"A Drive file named {name!r} exists with a different size; "
                "use duplicate_policy=rename to keep both."
            )
        stem, ext = os.path.splitext(name)
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{stem} (quark-{suffix}){ext}", existing[0]

    def initiate_upload(
        self,
        name: str,
        size: int,
        parent_id: str,
        mime_type: str,
    ) -> str:
        response = self._request(
            "POST",
            DRIVE_UPLOAD + "/files",
            params={
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,size,mimeType,parents,md5Checksum",
            },
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size),
            },
            json={"name": name, "mimeType": mime_type, "parents": [parent_id]},
        )
        location = response.headers.get("Location")
        if not response.ok or not location:
            raise DriveError(
                f"Failed to initiate Drive resumable upload with HTTP {response.status_code}"
            )
        return location

    def put_chunk(
        self,
        session_url: str,
        *,
        start: int,
        total: int,
        data: bytes,
        max_retries: int = 6,
    ) -> requests.Response:
        end = start + len(data) - 1
        for attempt in range(max_retries):
            response = self.session.put(
                session_url,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Type": "application/octet-stream",
                },
                data=data,
                timeout=max(self.timeout, 180),
            )
            if response.status_code in (200, 201, 308):
                return response
            if response.status_code == 401:
                self._token(force=True)
            elif response.status_code not in (408, 429) and not (
                500 <= response.status_code < 600
            ):
                raise DriveError(
                    f"Drive chunk upload failed with HTTP {response.status_code}"
                )
            time.sleep(min(2**attempt, 30))
        raise DriveError(f"Drive chunk upload failed after {max_retries} retries")

    def get_file(self, file_id: str) -> Dict[str, Any]:
        response = self._request(
            "GET",
            DRIVE_API + f"/files/{file_id}",
            params={
                "fields": "id,name,size,mimeType,parents,md5Checksum",
                "supportsAllDrives": "true",
            },
        )
        if not response.ok:
            raise DriveError(
                f"Google Drive verification failed with HTTP {response.status_code}"
            )
        return dict(response.json())
