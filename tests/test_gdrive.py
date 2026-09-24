from __future__ import annotations

from typing import Any

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
