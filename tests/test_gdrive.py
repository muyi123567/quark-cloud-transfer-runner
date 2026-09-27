from __future__ import annotations

from typing import Any

import pytest

from quark_cloud_transfer.errors import DriveError
from quark_cloud_transfer.gdrive import GoogleDriveClient


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.post_calls: list[dict[str, Any]] = []
        self.request_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.token_response = FakeResponse(
            payload={"access_token": "access", "expires_in": 3600}
        )
        self.request_response = FakeResponse()
        self.put_response = FakeResponse(status_code=308)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self.token_response

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.request_calls.append({"method": method, "url": url, **kwargs})
        return self.request_response

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        self.put_calls.append({"url": url, **kwargs})
        return self.put_response


def make_client(session: FakeSession) -> GoogleDriveClient:
    return GoogleDriveClient(
        {
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "refresh",
        },
        session=session,  # type: ignore[arg-type]
    )


def test_refreshes_access_token_without_returning_secret() -> None:
    session = FakeSession()
    client = make_client(session)
    assert client._token() == "access"
    assert session.post_calls[0]["url"].endswith("/token")


def test_choose_destination_skips_same_name_and_size() -> None:
    client = make_client(FakeSession())
    client.list_named = lambda *args, **kwargs: [  # type: ignore[method-assign]
        {"id": "existing", "name": "a.pdf", "size": "123"}
    ]
    chosen, existing = client.choose_destination(
        "parent",
        "a.pdf",
        123,
        on_exists="skip",
    )
    assert chosen is None
    assert existing is not None and existing["id"] == "existing"


def test_put_chunk_sends_resumable_content_range() -> None:
    session = FakeSession()
    client = make_client(session)
    client._access_token = "access"
    client._expires_at = 10**12
    response = client.put_chunk(
        "https://upload.example/session",
        start=0,
        total=4,
        data=b"test",
    )
    assert response.status_code == 308
    assert session.put_calls[0]["headers"]["Content-Range"] == "bytes 0-3/4"


def test_uploaded_bytes_reads_the_committed_offset() -> None:
    session = FakeSession()
    session.put_response = FakeResponse(
        status_code=308,
        headers={"Range": "bytes=0-2047"},
    )
    client = make_client(session)
    client._access_token = "access"
    client._expires_at = 10**12

    assert client.uploaded_bytes("https://upload.example/session", 4096) == 2048
    assert (
        session.put_calls[0]["headers"]["Content-Range"] == "bytes */4096"
    )


def test_uploaded_bytes_treats_a_fresh_session_as_empty() -> None:
    session = FakeSession()
    session.put_response = FakeResponse(status_code=308)
    client = make_client(session)
    client._access_token = "access"
    client._expires_at = 10**12

    assert client.uploaded_bytes("https://upload.example/session", 4096) == 0


def test_uploaded_bytes_reports_a_completed_session() -> None:
    session = FakeSession()
    session.put_response = FakeResponse(status_code=200, payload={"id": "drive-1"})
    client = make_client(session)
    client._access_token = "access"
    client._expires_at = 10**12

    assert client.uploaded_bytes("https://upload.example/session", 4096) == 4096


def test_uploaded_bytes_raises_when_the_session_is_gone() -> None:
    session = FakeSession()
    session.put_response = FakeResponse(status_code=404)
    client = make_client(session)
    client._access_token = "access"
    client._expires_at = 10**12

    with pytest.raises(DriveError, match="session is gone"):
        client.uploaded_bytes("https://upload.example/session", 4096)


def test_initiate_update_uses_resumable_patch() -> None:
    session = FakeSession()
    session.request_response = FakeResponse(
        status_code=200,
        headers={"Location": "https://upload.example/update-session"},
    )
    client = make_client(session)
    client._access_token = "access"
    client._expires_at = 10**12

    url = client.initiate_update("file-1", 123, "application/pdf")

    assert url == "https://upload.example/update-session"
    call = session.request_calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/upload/drive/v3/files/file-1")
    assert call["params"]["uploadType"] == "resumable"
    assert call["headers"]["X-Upload-Content-Length"] == "123"
