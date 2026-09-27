"""Quark CDN resilience: file-level retry, fresh URLs, resume, and summaries."""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest
import requests

import quark_cloud_transfer.cli as cli
from quark_cloud_transfer.errors import DriveError, QuarkCdnError
from quark_cloud_transfer.models import QuarkItem, TransferResult
from quark_cloud_transfer.transfer import TransferService


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[dict[str, Any]] = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeStream:
    """Context-manager stand-in for a streamed Quark CDN response."""

    def __init__(self, data: bytes, *, truncate_at: Optional[int] = None) -> None:
        self.data = data
        self.truncate_at = truncate_at

    def iter_content(self, size: int):
        if self.truncate_at is not None:
            payload = self.data[: self.truncate_at]
            for index in range(0, len(payload), max(size, 1)):
                yield payload[index : index + max(size, 1)]
            return
        for index in range(0, len(self.data), max(size, 1)):
            yield self.data[index : index + max(size, 1)]

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class ResilientQuark:
    """Quark client whose CDN leg fails a scripted number of times.

    Every ``get_download_items`` call hands back a different URL, which is what
    the file-level retry is expected to use.
    """

    def __init__(
        self,
        items: list[QuarkItem],
        *,
        probe_failures: int = 0,
        stream_failures: int = 0,
        range_supported: bool = True,
        dead_fids: frozenset[str] = frozenset(),
    ) -> None:
        self._items = {item.fid: item for item in items}
        self.probe_failures = probe_failures
        self.stream_failures = stream_failures
        self.range_supported = range_supported
        self.dead_fids = dead_fids
        self.download_requests: list[tuple[str, str]] = []
        self.probe_urls: list[str] = []
        self.stream_urls: list[str] = []
        self.range_calls: list[tuple[str, int, int]] = []
        self.stream_opened: list[str] = []
        self._stream_attempts = 0

    def search(self, *args: Any, **kwargs: Any) -> list[QuarkItem]:
        return list(self._items.values())

    def get_download_items(self, fids: list[str]) -> list[dict[str, Any]]:
        fid = fids[0]
        item = self._items[fid]
        url = f"https://dl-pc-zb.drive.quark.cn/{fid}/{len(self.download_requests)}"
        self.download_requests.append((fid, url))
        return [
            {
                "file_name": item.name,
                "size": item.size,
                "download_url": url,
            }
        ]

    def _fid_for(self, url: str) -> str:
        return url.rsplit("/", 2)[1]

    def probe_range(self, url: str, size: int) -> bool:
        self.probe_urls.append(url)
        if self._fid_for(url) in self.dead_fids:
            raise QuarkCdnError(
                "HTTPSConnectionPool(host='dl-pc-zb.drive.quark.cn', port=443): "
                "ConnectTimeoutError(connect timeout=20)"
            )
        if self.probe_failures > 0:
            self.probe_failures -= 1
            raise QuarkCdnError(
                "HTTPSConnectionPool(host='dl-pc-zb.drive.quark.cn', port=443): "
                "Connection to dl-pc-zb.drive.quark.cn timed out. (connect timeout=20)"
            )
        return self.range_supported

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        self.range_calls.append((url, start, end))
        return bytes([start % 251]) * (end - start + 1)

    def open_stream(self, url: str) -> FakeStream:
        self.stream_opened.append(url)
        self._stream_attempts += 1
        item = self._items[self._fid_for(url)]
        payload = bytes([(index + 7) % 251 for index in range(item.size)])
        truncate_at = None
        if self.stream_failures > 0:
            self.stream_failures -= 1
            truncate_at = max(item.size // 2, 1)
        return FakeStream(payload, truncate_at=truncate_at)


class RecordingDrive:
    """Drive fake that tracks resumable sessions and committed offsets."""

    def __init__(
        self,
        *,
        committed: int = 0,
        skip_on_second_choice: bool = False,
        status_probe_error: Optional[Exception] = None,
    ) -> None:
        self.committed = committed
        self.skip_on_second_choice = skip_on_second_choice
        self.status_probe_error = status_probe_error
        self.sessions: list[tuple[str, int, str]] = []
        self.chunks: list[tuple[str, int, int, bytes]] = []
        self.status_probes: list[str] = []
        self.choose_calls: list[str] = []
        self._name = "a.bin"
        self._size = 0

    def ensure_folder_path(self, destination: str, *, root_id: str) -> str:
        return "parent"

    def choose_destination(
        self,
        parent_id: str,
        name: str,
        size: int,
        *,
        on_exists: str,
    ):
        self.choose_calls.append(name)
        if self.skip_on_second_choice and len(self.choose_calls) > 1:
            return None, {"id": "existing", "name": name, "size": str(size)}
        return name, None

    def initiate_upload(self, name: str, size: int, parent_id: str, mime_type: str) -> str:
        self._name = name
        self._size = size
        url = f"https://upload.example/session-{len(self.sessions)}"
        self.sessions.append((name, size, parent_id))
        return url

    def uploaded_bytes(self, session_url: str, total: int) -> int:
        self.status_probes.append(session_url)
        if self.status_probe_error is not None:
            raise self.status_probe_error
        return self.committed

    def put_chunk(
        self,
        session_url: str,
        *,
        start: int,
        total: int,
        data: bytes,
    ) -> FakeResponse:
        self.chunks.append((session_url, start, total, data))
        if start + len(data) == total:
            return FakeResponse(200, {"id": "drive-1", "name": self._name, "size": total})
        return FakeResponse(308, {})

    def get_file(self, file_id: str) -> dict[str, Any]:
        return {"id": file_id, "name": self._name, "size": self._size}


def make_service(
    quark: Any,
    drive: Any,
    *,
    chunk_size: int = 4,
    file_attempts: int = 6,
    retry_rounds: int = 0,
    retry_round_delay_seconds: float = 0.0,
    max_runtime_seconds: float = 0.0,
    sleep: Any = None,
    clock: Any = None,
    events: Optional[list[tuple[str, dict[str, Any]]]] = None,
) -> TransferService:
    recorded = events if events is not None else []

    def emit(event: str, **payload: Any) -> None:
        recorded.append((event, payload))

    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if clock is not None:
        kwargs["clock"] = clock
    return TransferService(
        quark,
        drive,
        chunk_size=chunk_size,
        max_files=10,
        max_depth=3,
        max_nodes=100,
        duplicate_policy="skip",
        emit=emit,
        file_attempts=file_attempts,
        retry_rounds=retry_rounds,
        retry_round_delay_seconds=retry_round_delay_seconds,
        max_runtime_seconds=max_runtime_seconds,
        **kwargs,
    )


def test_range_path_retries_with_fresh_url_and_exponential_backoff() -> None:
    item = QuarkItem("fid-1", "a.bin", "/a.bin", 4, False)
    quark = ResilientQuark([item], probe_failures=2)
    drive = RecordingDrive()
    sleeps: list[float] = []
    events: list[tuple[str, dict[str, Any]]] = []
    service = make_service(quark, drive, sleep=sleeps.append, events=events)

    results = service.run(query="a", destination="inbox", dry_run=False)

    assert results[0].status == "ok"
    # One URL per attempt, all different.
    urls = [url for _, url in quark.download_requests]
    assert len(urls) == 3
    assert len(set(urls)) == 3
    # 5s then 15s of backoff before attempts 2 and 3.
    assert sleeps == [5.0, 15.0]
    retries = [payload for event, payload in events if event == "file_retry"]
    assert [payload["attempt"] for payload in retries] == [2, 3]
    assert [payload["delay_seconds"] for payload in retries] == [5.0, 15.0]
    assert all("timed out" in str(payload["error"]) for payload in retries)
    # The resumable session is reused rather than recreated.
    assert len(drive.sessions) == 1
    assert quark.range_calls == [(urls[2], 0, 3)]


def test_stream_path_retries_and_resumes_at_committed_offset() -> None:
    item = QuarkItem("fid-1", "a.bin", "/a.bin", 5, False)
    quark = ResilientQuark([item], range_supported=False, stream_failures=1)
    drive = RecordingDrive(committed=2)
    sleeps: list[float] = []
    events: list[tuple[str, dict[str, Any]]] = []
    service = make_service(
        quark,
        drive,
        chunk_size=2,
        sleep=sleeps.append,
        events=events,
    )

    results = service.run(query="a", destination="inbox", dry_run=False)

    assert results[0].status == "ok"
    assert sleeps == [5.0]
    # The second attempt asks for a new URL and reuses the same session.
    assert len(quark.stream_opened) == 2
    assert quark.stream_opened[0] != quark.stream_opened[1]
    assert drive.status_probes == [drive.chunks[0][0]]
    # Resume skips the two already-committed bytes instead of restarting.
    payload = bytes([(index + 7) % 251 for index in range(5)])
    assert [(start, data) for _, start, _, data in drive.chunks] == [
        (0, payload[0:2]),
        (2, payload[2:4]),
        (4, payload[4:5]),
    ]
    probes = [payload for event, payload in events if event == "source_probe"]
    assert [payload["resume_from"] for payload in probes] == [0, 2]


def test_single_file_failure_does_not_abort_the_batch() -> None:
    items = [
        QuarkItem("fid-1", "a.bin", "/a.bin", 4, False),
        QuarkItem("fid-2", "b.bin", "/b.bin", 4, False),
        QuarkItem("fid-3", "c.bin", "/c.bin", 4, False),
    ]
    quark = ResilientQuark(items, dead_fids=frozenset({"fid-2"}))
    drive = RecordingDrive()
    sleeps: list[float] = []
    events: list[tuple[str, dict[str, Any]]] = []
    service = make_service(
        quark,
        drive,
        file_attempts=2,
        sleep=sleeps.append,
        events=events,
    )

    results = service.run(query="a", destination="inbox", dry_run=False)

    assert [result.status for result in results] == ["ok", "failed", "ok"]
    assert results[1].error is not None and "ConnectTimeout" in results[1].error
    # The file after the dead one was still attempted.
    assert [fid for fid, _ in quark.download_requests] == [
        "fid-1",
        "fid-2",
        "fid-2",
        "fid-3",
    ]
    assert [event for event, _ in events if event == "file_failed"] == ["file_failed"]
    summary = [payload for event, payload in events if event == "summary"][0]
    assert summary["total"] == 3
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    assert summary["failures"][0]["name"] == "b.bin"


def test_retry_rounds_revisit_the_failed_set() -> None:
    item = QuarkItem("fid-1", "a.bin", "/a.bin", 4, False)
    quark = ResilientQuark([item], probe_failures=1)
    drive = RecordingDrive()
    events: list[tuple[str, dict[str, Any]]] = []
    service = make_service(
        quark,
        drive,
        file_attempts=1,
        retry_rounds=1,
        events=events,
    )

    results = service.run(query="a", destination="inbox", dry_run=False)

    assert results[0].status == "ok"
    assert [event for event, _ in events].count("retry_round_start") == 1
    rounds = [payload for event, payload in events if event == "retry_round_start"]
    assert rounds[0]["files"] == 1
    summary = [payload for event, payload in events if event == "summary"][0]
    assert summary["failed"] == 0


def test_retry_keeps_same_name_same_size_skip_semantics() -> None:
    item = QuarkItem("fid-1", "a.bin", "/a.bin", 5, False)
    quark = ResilientQuark([item], range_supported=False, stream_failures=1)
    drive = RecordingDrive(
        status_probe_error=DriveError("Drive resumable upload session is gone"),
        skip_on_second_choice=True,
    )
    service = make_service(quark, drive, chunk_size=2, sleep=lambda _: None)

    results = service.run(query="a", destination="inbox", dry_run=False)

    # A later attempt found the file already complete and skipped it instead of
    # uploading a duplicate.
    assert results[0].status == "skipped"
    assert results[0].drive_id == "existing"
    assert len(drive.sessions) == 1


def test_runtime_budget_defers_remaining_files() -> None:
    items = [
        QuarkItem("fid-1", "a.bin", "/a.bin", 4, False),
        QuarkItem("fid-2", "b.bin", "/b.bin", 4, False),
        QuarkItem("fid-3", "c.bin", "/c.bin", 4, False),
    ]
    quark = ResilientQuark(items)
    drive = RecordingDrive()
    events: list[tuple[str, dict[str, Any]]] = []
    ticks = iter([0.0, 0.0, 500.0, 500.0, 500.0, 500.0])
    service = make_service(
        quark,
        drive,
        max_runtime_seconds=60.0,
        clock=lambda: next(ticks, 500.0),
        events=events,
    )

    results = service.run(query="a", destination="inbox", dry_run=False)

    assert [result.status for result in results] == ["ok", "deferred", "deferred"]
    assert [event for event, _ in events if event == "runtime_budget_exhausted"]
    summary = [payload for event, payload in events if event == "summary"][0]
    assert summary["deferred"] == 2
    assert [entry["name"] for entry in summary["deferred_files"]] == ["b.bin", "c.bin"]


class StubService:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def run(self, **kwargs: Any) -> list[TransferResult]:
        return StubService.results


def run_cli(monkeypatch: pytest.MonkeyPatch, results: list[TransferResult], capsys) -> tuple[int, str]:
    StubService.results = results
    monkeypatch.setattr(cli, "TransferService", StubService)
    monkeypatch.setenv("QUARK_COOKIE", "auth=1; __puus=secret")
    monkeypatch.setenv(
        "GDRIVE_OAUTH_JSON",
        json.dumps(
            {
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh",
            }
        ),
    )
    code = cli.main(["--query", "a", "--destination", "inbox"])
    return code, capsys.readouterr().out


def test_cli_exits_zero_when_every_file_landed(monkeypatch, capsys) -> None:
    code, out = run_cli(
        monkeypatch,
        [
            TransferResult(status="ok", name="a.bin", source_size=1, source_path="/a.bin"),
            TransferResult(
                status="skipped",
                name="b.bin",
                source_size=1,
                source_path="/b.bin",
            ),
        ],
        capsys,
    )
    assert code == 0
    assert "incomplete" not in out


def test_cli_exits_non_zero_and_reports_leftovers(monkeypatch, capsys) -> None:
    code, out = run_cli(
        monkeypatch,
        [
            TransferResult(status="ok", name="a.bin", source_size=1, source_path="/a.bin"),
            TransferResult(
                status="failed",
                name="b.bin",
                source_size=1,
                source_path="/b.bin",
                error="ConnectTimeout",
            ),
            TransferResult(
                status="deferred",
                name="c.bin",
                source_size=1,
                source_path="/c.bin",
                error="run time budget exhausted",
            ),
        ],
        capsys,
    )
    assert code == 3
    assert '"event": "incomplete"' in out
    assert '"failed": 1' in out
    assert '"deferred": 1' in out


def test_transfer_retries_raw_connection_errors() -> None:
    """A raw requests.ConnectionError on the Quark API leg is retryable too."""

    class FlakyQuark(ResilientQuark):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.calls = 0

        def get_download_items(self, fids: list[str]) -> list[dict[str, Any]]:
            self.calls += 1
            if self.calls == 1:
                raise requests.exceptions.ConnectionError("connection reset by peer")
            return super().get_download_items(fids)

    item = QuarkItem("fid-1", "a.bin", "/a.bin", 4, False)
    quark = FlakyQuark([item])
    drive = RecordingDrive()
    sleeps: list[float] = []
    service = make_service(quark, drive, sleep=sleeps.append)

    results = service.run(query="a", destination="inbox", dry_run=False)

    assert results[0].status == "ok"
    assert sleeps == [5.0]
    assert quark.calls == 2


def test_cli_wires_the_resilience_knobs_into_the_service(monkeypatch, capsys) -> None:
    captured: dict[str, Any] = {}

    class RecordingService:
        def __init__(self, quark: Any, drive: Any, **kwargs: Any) -> None:
            captured.update(kwargs)
            captured["quark"] = quark

        def run(self, **kwargs: Any) -> list[TransferResult]:
            return [TransferResult(status="ok", name="a", source_size=1, source_path="/a")]

    monkeypatch.setattr(cli, "TransferService", RecordingService)
    monkeypatch.setattr(cli, "QuarkClient", lambda cookie, **kwargs: ("quark", kwargs))
    monkeypatch.setattr(cli, "GoogleDriveClient", lambda oauth: "drive")
    monkeypatch.setenv("QUARK_COOKIE", "auth=1; __puus=secret")
    monkeypatch.setenv(
        "GDRIVE_OAUTH_JSON",
        json.dumps(
            {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        ),
    )

    code = cli.main(
        [
            "--query",
            "a",
            "--destination",
            "inbox",
            "--file-attempts",
            "4",
            "--retry-rounds",
            "3",
            "--retry-round-delay",
            "7",
            "--max-runtime-seconds",
            "120",
            "--cdn-connect-timeout",
            "9",
            "--cdn-read-timeout",
            "60",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert captured["file_attempts"] == 4
    assert captured["retry_rounds"] == 3
    assert captured["retry_round_delay_seconds"] == 7.0
    assert captured["max_runtime_seconds"] == 120.0
    assert captured["quark"] == (
        "quark",
        {"cdn_connect_timeout": 9.0, "cdn_read_timeout": 60.0},
    )


def test_cli_runs_a_real_service_against_fake_clients(monkeypatch, capsys) -> None:
    item = QuarkItem("fid-1", "a.bin", "/a.bin", 4, False)
    quark = ResilientQuark([item])
    drive = RecordingDrive()
    monkeypatch.setattr(cli, "QuarkClient", lambda cookie, **kwargs: quark)
    monkeypatch.setattr(cli, "GoogleDriveClient", lambda oauth: drive)
    monkeypatch.setenv("QUARK_COOKIE", "auth=1; __puus=secret")
    monkeypatch.setenv(
        "GDRIVE_OAUTH_JSON",
        json.dumps(
            {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        ),
    )

    code = cli.main(
        [
            "--query",
            "a",
            "--destination",
            "inbox",
            "--chunk-mib",
            "1",
            "--file-attempts",
            "2",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert '"event": "summary"' in out
    assert '"ok": 1' in out
    assert drive.chunks and drive.chunks[0][1] == 0
