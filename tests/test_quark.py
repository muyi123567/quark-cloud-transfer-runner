from __future__ import annotations

from typing import Any

import pytest
import requests

from quark_cloud_transfer.errors import QuarkCdnError
from quark_cloud_transfer.models import QuarkItem
from quark_cloud_transfer.quark import (
    CDN_CONNECT_TIMEOUT_SECONDS,
    CDN_READ_TIMEOUT_SECONDS,
    QUARK_DOWNLOAD,
    QUARK_UA,
    QuarkClient,
)


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.cookies = cookies or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def iter_content(self, size: int):
        yield from self._chunks

    def close(self) -> None:
        return None

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self._next()

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self._next()

    def _next(self) -> FakeResponse:
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_resolve_path_uses_exact_segment_names() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse(
            {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "fid": "folder-1",
                            "file_name": "数学",
                            "file_type": 0,
                            "size": 0,
                        }
                    ]
                },
            }
        ),
        FakeResponse(
            {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "fid": "file-1",
                            "file_name": "武忠祥.pdf",
                            "file_type": 1,
                            "size": 123,
                        }
                    ]
                },
            }
        ),
    ]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]
    item = client.resolve_path("/数学/武忠祥.pdf")
    assert item.fid == "file-1"
    assert item.name == "武忠祥.pdf"


def test_search_uses_global_endpoint_and_filters_files() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse(
            {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "fid": "d1",
                            "file_name": "武忠祥资料",
                            "file_type": 0,
                            "size": 0,
                        },
                        {
                            "fid": "f1",
                            "file_name": "武忠祥基础.pdf",
                            "file_type": 1,
                            "size": 10,
                            "file_path": "/转存的内容/武忠祥基础.pdf",
                        },
                        {
                            "fid": "f2",
                            "file_name": "英语.pdf",
                            "file_type": 1,
                            "size": 11,
                        },
                    ]
                },
            }
        )
    ]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    hits = client.search("武忠祥", max_depth=2)

    assert [item.fid for item in hits] == ["f1"]
    assert hits[0].path == "/转存的内容/武忠祥基础.pdf"
    assert session.get_calls[0]["url"].endswith("/file/search")
    assert session.get_calls[0]["params"]["q"] == "武忠祥"


def test_probe_range_checks_content_range_total() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse(
            status_code=206,
            headers={"Content-Range": "bytes 0-0/123"},
            chunks=[b"x"],
        ),
        FakeResponse(
            status_code=206,
            headers={"Content-Range": "bytes 0-0/123"},
            chunks=[b"x"],
        ),
    ]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]
    assert client.probe_range("https://download.example/file", 123) is True
    assert client.probe_range("https://download.example/file", 999) is False


def test_pc_download_contract() -> None:
    assert QUARK_DOWNLOAD == "https://drive-pc.quark.cn/1/clouddrive/file/download"
    assert "quark-cloud-drive/" in QUARK_UA
    assert "Electron/" in QUARK_UA
    assert "Channel/pckk_other_ch" in QUARK_UA


def test_refresh_session_rotates_puus_without_dropping_login_cookie() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse({"code": 0, "data": {}}, cookies={"__puus": "fresh", "__pus": "fresh-pus"})
    ]
    client = QuarkClient("auth=keep; __puus=stale; __pus=old-pus", session=session)  # type: ignore[arg-type]

    client.refresh_session()

    sent_cookie = session.get_calls[0]["headers"]["Cookie"]
    assert "auth=keep" in sent_cookie
    assert "__puus=" not in sent_cookie
    assert "__pus=old-pus" in sent_cookie
    assert "__puus=fresh" in client.cookie
    assert "__pus=fresh-pus" in client.cookie


def test_download_link_request_uses_rotated_cookie() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse({"code": 0, "data": {}}, cookies={"__puus": "fresh"}),
        FakeResponse(
            {
                "code": 0,
                "data": [
                    {
                        "fid": "file-1",
                        "file_name": "a.bin",
                        "size": 5,
                        "download_url": "https://download.example/a.bin",
                    }
                ],
            }
        ),
    ]
    client = QuarkClient("auth=keep; __puus=stale", session=session)  # type: ignore[arg-type]

    items = client.get_download_items(["file-1"])

    assert items[0]["fid"] == "file-1"
    assert "__puus=fresh" in session.post_calls[0]["headers"]["Cookie"]
    assert "__puus=stale" not in session.post_calls[0]["headers"]["Cookie"]


def test_cdn_requests_use_short_connect_and_long_read_timeout() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse(
            status_code=206,
            headers={"Content-Range": "bytes 0-0/123"},
            chunks=[b"x"],
        )
    ]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    assert client.probe_range("https://dl-pc-zb.drive.quark.cn/x", 123) is True

    # A short connect timeout lets a bad CDN node fail over quickly instead of
    # burning two minutes of the run window per attempt.
    assert session.get_calls[0]["timeout"] == (
        CDN_CONNECT_TIMEOUT_SECONDS,
        CDN_READ_TIMEOUT_SECONDS,
    )
    assert CDN_CONNECT_TIMEOUT_SECONDS < CDN_READ_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectTimeout("connection to CDN timed out"),
        requests.exceptions.ReadTimeout("read from CDN timed out"),
        requests.exceptions.ConnectionError("connection reset by peer"),
    ],
)
def test_probe_range_surfaces_transport_failures_as_cdn_errors(error: Exception) -> None:
    session = FakeSession()
    session.responses = [error]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    with pytest.raises(QuarkCdnError, match="range probe failed"):
        client.probe_range("https://dl-pc-zb.drive.quark.cn/x", 10)


def test_probe_range_flags_an_expired_signed_url() -> None:
    session = FakeSession()
    session.responses = [FakeResponse(status_code=412)]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    with pytest.raises(QuarkCdnError, match="HTTP 412"):
        client.probe_range("https://dl-pc-zb.drive.quark.cn/x", 10)


def test_fetch_range_surfaces_transport_failures_as_cdn_errors() -> None:
    session = FakeSession()
    session.responses = [requests.exceptions.ReadTimeout("read timed out")]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    with pytest.raises(QuarkCdnError, match="Range 0-9 fetch failed"):
        client.fetch_range("https://dl-pc-zb.drive.quark.cn/x", 0, 9)


def test_fetch_range_flags_a_short_body() -> None:
    session = FakeSession()
    session.responses = [
        FakeResponse(status_code=206, chunks=[b"ab"]),
    ]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    with pytest.raises(QuarkCdnError, match="length mismatch"):
        client.fetch_range("https://dl-pc-zb.drive.quark.cn/x", 0, 9)


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectTimeout("connection to CDN timed out"),
        requests.exceptions.ConnectionError("connection reset by peer"),
    ],
)
def test_open_stream_surfaces_transport_failures_as_cdn_errors(error: Exception) -> None:
    session = FakeSession()
    session.responses = [error]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    with pytest.raises(QuarkCdnError, match="stream connection failed"):
        client.open_stream("https://dl-pc-zb.drive.quark.cn/x")


def test_open_stream_flags_an_expired_signed_url() -> None:
    session = FakeSession()
    session.responses = [FakeResponse(status_code=412)]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    with pytest.raises(QuarkCdnError, match="HTTP 412"):
        client.open_stream("https://dl-pc-zb.drive.quark.cn/x")


def test_open_stream_returns_a_live_response() -> None:
    session = FakeSession()
    session.responses = [FakeResponse(status_code=200, chunks=[b"data"])]
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    response = client.open_stream("https://dl-pc-zb.drive.quark.cn/x")

    assert response.status_code == 200


def test_explicit_proxy_is_applied_to_the_session() -> None:
    session = FakeSession()
    QuarkClient(
        "__puus=secret",
        session=session,  # type: ignore[arg-type]
        proxy="http://user:pass@proxy.example:3128",
    )

    # An explicit proxy is honored even though trust_env stays False, so only
    # the Quark leg is routed through it.
    assert session.proxies["http"] == "http://user:pass@proxy.example:3128"
    assert session.proxies["https"] == "http://user:pass@proxy.example:3128"
    assert session.trust_env is False


def test_no_proxy_keeps_the_session_direct() -> None:
    session = FakeSession()
    client = QuarkClient("__puus=secret", session=session)  # type: ignore[arg-type]

    assert session.proxies == {}
    assert client.proxy is None
