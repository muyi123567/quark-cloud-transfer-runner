from __future__ import annotations

from typing import Any

from quark_cloud_transfer.models import QuarkItem
from quark_cloud_transfer.quark import QuarkClient


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def iter_content(self, size: int):
        yield from self._chunks

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.responses: list[FakeResponse] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


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


def test_search_is_recursive_and_files_only() -> None:
    client = QuarkClient("__puus=secret", session=FakeSession())  # type: ignore[arg-type]

    def fake_list_dir(parent_fid: str, page_size: int = 100):
        if parent_fid == "0":
            yield QuarkItem("d1", "数学", "/数学", 0, True)
        else:
            yield QuarkItem("f1", "武忠祥基础.pdf", "/数学/武忠祥基础.pdf", 10, False)
            yield QuarkItem("f2", "英语.pdf", "/数学/英语.pdf", 11, False)

    client.list_dir = fake_list_dir  # type: ignore[method-assign]
    hits = client.search("武忠祥", max_depth=2)
    assert [item.fid for item in hits] == ["f1"]


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
