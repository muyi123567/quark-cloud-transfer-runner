from __future__ import annotations

from typing import Any

from quark_cloud_transfer.models import QuarkItem
from quark_cloud_transfer.transfer import TransferService


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeQuark:
    def __init__(self) -> None:
        self.range_calls: list[tuple[int, int]] = []

    def search(self, *args: Any, **kwargs: Any) -> list[QuarkItem]:
        return [QuarkItem("fid-1", "a.bin", "/a.bin", 5, False)]

    def get_download_items(self, fids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "file_name": "a.bin",
                "size": 5,
                "download_url": "https://download.example/a",
            }
        ]

    def probe_range(self, url: str, size: int) -> bool:
        return True

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        self.range_calls.append((start, end))
        return b"x" * (end - start + 1)


class FakeDrive:
    def __init__(self) -> None:
        self.chunks: list[tuple[int, int, bytes]] = []
        self.folder_calls: list[tuple[str, str]] = []
        self.upload_parent_ids: list[str] = []

    def ensure_folder_path(self, destination: str, *, root_id: str) -> str:
        self.folder_calls.append((destination, root_id))
        if root_id == "parent":
            return "sub-parent"
        return "parent"

    def choose_destination(
        self,
        parent_id: str,
        name: str,
        size: int,
        *,
        on_exists: str,
    ):
        return name, None

    def initiate_upload(
        self,
        name: str,
        size: int,
        parent_id: str,
        mime_type: str,
    ) -> str:
        self.upload_parent_ids.append(parent_id)
        return "https://upload.example/session"

    def put_chunk(
        self,
        session_url: str,
        *,
        start: int,
        total: int,
        data: bytes,
    ) -> FakeResponse:
        self.chunks.append((start, total, data))
        if start + len(data) == total:
            return FakeResponse({"id": "drive-1", "name": "a.bin", "size": total})
        return FakeResponse({"id": "drive-1"}, status_code=308)

    def get_file(self, file_id: str) -> dict[str, Any]:
        return {"id": file_id, "name": "a.bin", "size": 5}


def test_range_upload_is_chunked_and_verified() -> None:
    quark = FakeQuark()
    drive = FakeDrive()
    events: list[str] = []
    service = TransferService(
        quark,  # type: ignore[arg-type]
        drive,  # type: ignore[arg-type]
        chunk_size=2,
        max_files=10,
        max_depth=3,
        max_nodes=100,
        duplicate_policy="skip",
        emit=lambda event, **payload: events.append(event),
    )
    results = service.run(
        query="a",
        destination="inbox",
        dry_run=False,
    )
    assert results[0].status == "ok"
    assert quark.range_calls == [(0, 1), (2, 3), (4, 4)]
    assert [item[2] for item in drive.chunks] == [b"xx", b"xx", b"x"]
    assert "verified" in events


def test_dry_run_does_not_touch_drive() -> None:
    quark = FakeQuark()
    events: list[str] = []
    service = TransferService(
        quark,  # type: ignore[arg-type]
        None,
        chunk_size=2,
        max_files=10,
        max_depth=3,
        max_nodes=100,
        duplicate_policy="skip",
        emit=lambda event, **payload: events.append(event),
    )
    results = service.run(
        query="a",
        destination="inbox",
        dry_run=True,
    )
    assert results[0].status == "planned"
    assert events == ["plan"]


class FakeFolderQuark(FakeQuark):
    def resolve_path(self, source_path: str, *, root_fid: str = "0") -> QuarkItem:
        return QuarkItem("dir-1", "folder", "/folder", 0, True)

    def expand(
        self,
        item: QuarkItem,
        *,
        max_depth: int,
        max_nodes: int,
    ) -> list[QuarkItem]:
        return [QuarkItem("fid-1", "a.bin", "/folder/sub/a.bin", 5, False)]


def test_source_folder_hierarchy_is_preserved() -> None:
    quark = FakeFolderQuark()
    drive = FakeDrive()
    service = TransferService(
        quark,  # type: ignore[arg-type]
        drive,  # type: ignore[arg-type]
        chunk_size=2,
        max_files=10,
        max_depth=3,
        max_nodes=100,
        duplicate_policy="skip",
        emit=lambda event, **payload: None,
    )
    results = service.run(
        source_path="/root/folder",
        destination="inbox",
        dry_run=False,
    )
    assert results[0].status == "ok"
    assert drive.folder_calls == [("inbox", "root"), ("sub", "parent")]
    assert drive.upload_parent_ids == ["sub-parent"]
