from __future__ import annotations

import mimetypes
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Lock
from typing import Any, Optional

import requests

from .errors import DriveError, QuarkCdnError, QuarkCloudTransferError, TransferError
from .gdrive import GoogleDriveClient
from .models import QuarkItem, TransferResult
from .quark import QuarkClient
from .redaction import redact_text

EventSink = Callable[..., None]

# Attempts per file. Each attempt re-requests a fresh Quark download URL, so a
# CDN node that is refusing connections is simply left behind.
DEFAULT_FILE_ATTEMPTS = 6
# Exponential backoff between those attempts, in seconds.
DEFAULT_FILE_BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0)
# Extra sweeps over the failed set after the first pass over every file.
DEFAULT_RETRY_ROUNDS = 2
DEFAULT_RETRY_ROUND_DELAY_SECONDS = 60.0
# Files transferred in parallel. Each file keeps its own download URL and its
# own Drive resumable session, so files are independent of each other.
DEFAULT_CONCURRENCY = 4
# Within one large file, several Range chunks may be fetched from the CDN at
# once; the Drive side still receives them strictly in order.
DEFAULT_PREFETCH = 3
DEFAULT_PREFETCH_MIN_BYTES = 256 * 1024 * 1024

# Failures that a fresh download URL plus backoff can plausibly repair:
# ConnectTimeout, ReadTimeout, ConnectionError, ChunkedEncodingError, and the
# expired-signed-URL responses surfaced as QuarkCdnError.
_RETRYABLE_FILE_ERRORS: tuple[type[BaseException], ...] = (
    QuarkCdnError,
    requests.RequestException,
)


def _is_connect_class_failure(error: BaseException) -> bool:
    """True when the CDN never accepted a connection, or rejected the URL.

    Those two cases are worth retrying immediately against a *different* node
    instead of sleeping first: the node itself is the problem.
    """

    seen: list[BaseException] = []
    current: Optional[BaseException] = error
    while current is not None and len(seen) < 4 and current not in seen:
        seen.append(current)
        if isinstance(current, requests.exceptions.ConnectionError):
            # Covers ConnectTimeout and ProxyError too.
            return True
        if "rejected the signed download URL" in str(current):
            return True
        current = current.__cause__
    return False


@dataclass
class _UploadState:
    """Resumable-upload bookkeeping that survives a failed CDN attempt."""

    session_url: Optional[str] = None
    session_size: Optional[int] = None
    uploaded: int = 0


class TransferService:
    def __init__(
        self,
        quark: QuarkClient,
        drive: Optional[GoogleDriveClient],
        *,
        chunk_size: int,
        max_files: int,
        max_depth: int,
        max_nodes: int,
        duplicate_policy: str,
        drive_root_id: str = "root",
        emit: EventSink,
        file_attempts: int = DEFAULT_FILE_ATTEMPTS,
        file_backoff_seconds: Sequence[float] = DEFAULT_FILE_BACKOFF_SECONDS,
        retry_rounds: int = DEFAULT_RETRY_ROUNDS,
        retry_round_delay_seconds: float = DEFAULT_RETRY_ROUND_DELAY_SECONDS,
        max_runtime_seconds: float = 0.0,
        concurrency: int = DEFAULT_CONCURRENCY,
        prefetch: int = DEFAULT_PREFETCH,
        prefetch_min_bytes: int = DEFAULT_PREFETCH_MIN_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if file_attempts < 1:
            raise TransferError("file_attempts must be at least 1")
        if retry_rounds < 0:
            raise TransferError("retry_rounds cannot be negative")
        if retry_round_delay_seconds < 0:
            raise TransferError("retry_round_delay_seconds cannot be negative")
        if max_runtime_seconds < 0:
            raise TransferError("max_runtime_seconds cannot be negative")
        if concurrency < 1:
            raise TransferError("concurrency must be at least 1")
        if prefetch < 1:
            raise TransferError("prefetch must be at least 1")
        if prefetch_min_bytes < 0:
            raise TransferError("prefetch_min_bytes cannot be negative")
        self.quark = quark
        self.drive = drive
        self.chunk_size = chunk_size
        self.max_files = max_files
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.duplicate_policy = duplicate_policy
        self.drive_root_id = drive_root_id
        # Workers share one event stream; keep JSON lines from interleaving.
        self._emit_lock = Lock()
        self._raw_emit = emit

        def emit_locked(event: str, **payload: Any) -> None:
            with self._emit_lock:
                self._raw_emit(event, **payload)

        self.emit = emit_locked
        self.file_attempts = file_attempts
        self.file_backoff_seconds = tuple(float(value) for value in file_backoff_seconds)
        self.retry_rounds = retry_rounds
        self.retry_round_delay_seconds = float(retry_round_delay_seconds)
        self.max_runtime_seconds = float(max_runtime_seconds)
        self.concurrency = concurrency
        self.prefetch = prefetch
        self.prefetch_min_bytes = prefetch_min_bytes
        self._folder_lock = Lock()
        self._sleep = sleep
        self._clock = clock

    def _prefetch_depth(self, size: int) -> int:
        """Parallel Range fetches to use for one file."""

        if self.prefetch <= 1 or size < self.prefetch_min_bytes:
            return 1
        return self.prefetch

    def _file_backoff(self, failed_attempt: int) -> float:
        """Backoff before retrying after ``failed_attempt`` (1-based)."""

        if not self.file_backoff_seconds:
            return 0.0
        index = min(max(failed_attempt - 1, 0), len(self.file_backoff_seconds) - 1)
        return self.file_backoff_seconds[index]

    def _runtime_exceeded(self, started: float) -> bool:
        if self.max_runtime_seconds <= 0:
            return False
        return (self._clock() - started) >= self.max_runtime_seconds

    def select_items(
        self,
        *,
        query: Optional[str] = None,
        source_path: Optional[str] = None,
        source_fid: Optional[str] = None,
        root_fid: str = "0",
    ) -> list[QuarkItem]:
        supplied = [bool(query), bool(source_path), bool(source_fid)]
        if sum(supplied) != 1:
            raise TransferError("Choose exactly one of query, source_path, or source_fid")

        if source_fid:
            metadata = self.quark.get_download_items([source_fid])[0]
            name = str(metadata.get("file_name") or metadata.get("name") or "unnamed.bin")
            items = [
                QuarkItem(
                    fid=source_fid,
                    name=name,
                    path=f"/{name}",
                    size=int(metadata.get("size") or 0),
                    is_dir=False,
                )
            ]
        elif source_path:
            item = self.quark.resolve_path(source_path, root_fid=root_fid)
            items = self.quark.expand(
                item,
                max_depth=self.max_depth,
                max_nodes=self.max_nodes,
            )
        else:
            items = self.quark.search(
                query or "",
                parent_fid=root_fid,
                max_depth=self.max_depth,
                max_nodes=self.max_nodes,
                files_only=True,
            )

        items = [item for item in items if not item.is_dir]
        if not items:
            raise TransferError("No matching Quark files were found")
        if len(items) > self.max_files:
            raise TransferError(
                f"Selection contains {len(items)} files, above max_files={self.max_files}. "
                "Narrow the query or raise max_files deliberately."
            )
        return items

    def run(
        self,
        *,
        destination: str,
        dry_run: bool,
        query: Optional[str] = None,
        source_path: Optional[str] = None,
        source_fid: Optional[str] = None,
        root_fid: str = "0",
    ) -> list[TransferResult]:
        items = self.select_items(
            query=query,
            source_path=source_path,
            source_fid=source_fid,
            root_fid=root_fid,
        )
        self.emit(
            "plan",
            dry_run=dry_run,
            files=len(items),
            total_bytes=sum(item.size for item in items),
            destination=destination,
            items=[
                {"name": item.name, "path": item.path, "size": item.size}
                for item in items
            ],
        )

        if dry_run:
            return [
                TransferResult(
                    status="planned",
                    name=item.name,
                    source_size=item.size,
                    source_path=item.path,
                )
                for item in items
            ]
        if self.drive is None:
            raise TransferError("Google Drive client is required for a non-dry-run transfer")

        started = self._clock()
        parent_id = self.drive.ensure_folder_path(
            destination,
            root_id=self.drive_root_id,
        )
        self.emit("drive_folder_ready", destination=destination, folder_id=parent_id)

        outcomes: dict[str, TransferResult] = {}
        folder_cache: dict[str, str] = {"": parent_id}
        source_root_path = ""
        if source_path:
            normalized_source = source_path.replace("\\", "/").rstrip("/")
            source_root_name = normalized_source.rsplit("/", 1)[-1]
            if source_root_name:
                source_root_path = f"/{source_root_name}"

        context = (parent_id, folder_cache, source_root_path)
        failed: list[QuarkItem] = []
        deferred: list[QuarkItem] = []
        budget_exhausted = False
        with ThreadPoolExecutor(
            max_workers=self.concurrency,
            thread_name_prefix="transfer",
        ) as pool:
            swept, failed, deferred, budget_exhausted = self._sweep(
                pool,
                items,
                context,
                started,
            )
            outcomes.update(swept)

            for round_number in range(1, self.retry_rounds + 1):
                if not failed or budget_exhausted:
                    break
                self.emit(
                    "retry_round_start",
                    round=round_number,
                    files=len(failed),
                    delay_seconds=self.retry_round_delay_seconds,
                )
                if self.retry_round_delay_seconds:
                    self._sleep(self.retry_round_delay_seconds)
                swept, failed, round_deferred, budget_exhausted = self._sweep(
                    pool,
                    failed,
                    context,
                    started,
                )
                outcomes.update(swept)
                deferred.extend(round_deferred)
                self.emit(
                    "retry_round_end",
                    round=round_number,
                    still_failed=len(failed),
                )

        for item in deferred:
            outcomes.setdefault(
                item.fid,
                TransferResult(
                    status="deferred",
                    name=item.name,
                    source_size=item.size,
                    source_path=item.path,
                    error="run time budget exhausted before this file was started",
                ),
            )

        results = [outcomes[item.fid] for item in items if item.fid in outcomes]
        self.emit(
            "complete",
            results=[
                {
                    "status": result.status,
                    "name": result.name,
                    "drive_id": result.drive_id,
                    "size": result.source_size,
                }
                for result in results
            ],
        )
        self._emit_summary(results, items=len(items), started=started)
        return results

    def _sweep(
        self,
        pool: ThreadPoolExecutor,
        items: list[QuarkItem],
        context: tuple[str, dict[str, str], str],
        started: float,
    ) -> tuple[dict[str, TransferResult], list[QuarkItem], list[QuarkItem], bool]:
        """Transfer ``items`` with bounded concurrency.

        Returns (results by fid, still-failed items, never-started items, and
        whether the run budget stopped the sweep).
        """

        results: dict[str, TransferResult] = {}
        failed: list[QuarkItem] = []
        deferred: list[QuarkItem] = []
        budget_exhausted = False
        pending = list(items)
        window: dict[Future, QuarkItem] = {}
        while pending or window:
            while pending and len(window) < self.concurrency:
                if self._runtime_exceeded(started):
                    deferred = list(pending)
                    pending = []
                    budget_exhausted = True
                    self.emit(
                        "runtime_budget_exhausted",
                        remaining=len(deferred),
                        max_runtime_seconds=self.max_runtime_seconds,
                        elapsed_seconds=round(self._clock() - started, 1),
                    )
                    break
                item = pending.pop(0)
                window[pool.submit(self._transfer_item, item, context)] = item
            if not window:
                break
            done, _ = wait(list(window), return_when=FIRST_COMPLETED)
            for future in done:
                item = window.pop(future)
                result = future.result()
                results[item.fid] = result
                if result.status == "failed":
                    failed.append(item)
        return results, failed, deferred, budget_exhausted

    def _transfer_item(
        self,
        item: QuarkItem,
        context: tuple[str, dict[str, str], str],
    ) -> TransferResult:
        """Worker entry point; never raises, so one file cannot kill the batch."""

        parent_id, folder_cache, source_root_path = context
        try:
            parent = self._parent_for(
                item,
                parent_id,
                folder_cache,
                source_root_path,
            )
            return self._transfer_one(item, parent)
        except Exception as exc:
            message = redact_text(exc)
            self.emit(
                "file_failed",
                name=item.name,
                source_path=item.path,
                size=item.size,
                error=message,
            )
            return TransferResult(
                status="failed",
                name=item.name,
                source_size=item.size,
                source_path=item.path,
                error=message,
            )

    def _emit_summary(
        self,
        results: list[TransferResult],
        *,
        items: int,
        started: float,
    ) -> None:
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        self.emit(
            "summary",
            total=items,
            ok=counts.get("ok", 0),
            skipped=counts.get("skipped", 0),
            failed=counts.get("failed", 0),
            deferred=counts.get("deferred", 0),
            elapsed_seconds=round(self._clock() - started, 1),
            file_attempts=self.file_attempts,
            retry_rounds=self.retry_rounds,
            failures=[
                {
                    "name": result.name,
                    "path": result.source_path,
                    "error": result.error,
                }
                for result in results
                if result.status == "failed"
            ],
            deferred_files=[
                {"name": result.name, "path": result.source_path}
                for result in results
                if result.status == "deferred"
            ],
        )

    def _parent_for(
        self,
        item: QuarkItem,
        parent_id: str,
        folder_cache: dict[str, str],
        source_root_path: str,
    ) -> str:
        if not source_root_path or not item.path.startswith(source_root_path + "/"):
            return parent_id
        relative_path = item.path[len(source_root_path) + 1 :]
        relative_parent = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
        if not relative_parent:
            return parent_id
        # Serialize the check-and-create so two workers cannot race into two
        # folders with the same name.
        with self._folder_lock:
            cached = folder_cache.get(relative_parent)
            if cached:
                return cached
            assert self.drive is not None
            created = self.drive.ensure_folder_path(relative_parent, root_id=parent_id)
            folder_cache[relative_parent] = created
        self.emit(
            "drive_subfolder_ready",
            source_relative_path=relative_parent,
            folder_id=created,
        )
        return created

    def _transfer_one(
        self,
        item: QuarkItem,
        parent_id: str,
        *,
        existing_drive_id: Optional[str] = None,
    ) -> TransferResult:
        """Transfer one file, retrying the Quark CDN leg with a fresh URL.

        Every attempt re-requests a download URL for the same FID and resumes
        the Drive resumable session at its committed offset. Failures are
        returned as a ``failed`` result instead of raised, so a single bad file
        cannot abort the remaining files in the batch.
        """
        assert self.drive is not None
        state = _UploadState()
        last_error: Optional[str] = None
        last_exception: Optional[BaseException] = None
        for attempt in range(1, self.file_attempts + 1):
            if attempt > 1:
                delay = self._file_backoff(attempt - 1)
                if (
                    delay
                    and attempt == 2
                    and last_exception is not None
                    and _is_connect_class_failure(last_exception)
                ):
                    # The node refused the connection: ask for a new URL and
                    # try immediately instead of sleeping on a dead host.
                    delay = 0.0
                self.emit(
                    "file_retry",
                    name=item.name,
                    source_path=item.path,
                    attempt=attempt,
                    max_attempts=self.file_attempts,
                    delay_seconds=delay,
                    resume_from=state.uploaded,
                    error=last_error,
                )
                self._sleep(delay)
            try:
                return self._attempt_file(
                    item,
                    parent_id,
                    state,
                    attempt,
                    existing_drive_id=existing_drive_id,
                )
            except _RETRYABLE_FILE_ERRORS as exc:
                last_error = redact_text(exc)
                last_exception = exc
                continue
            except QuarkCloudTransferError as exc:
                # A deterministic failure (bad metadata, Drive duplicate policy,
                # Drive rejection) will not improve with a fresh URL.
                last_error = redact_text(exc)
                last_exception = exc
                break
        self.emit(
            "file_failed",
            name=item.name,
            source_path=item.path,
            size=item.size,
            attempts=self.file_attempts,
            error=last_error,
        )
        return TransferResult(
            status="failed",
            name=item.name,
            source_size=item.size,
            source_path=item.path,
            error=last_error,
        )

    def _attempt_file(
        self,
        item: QuarkItem,
        parent_id: str,
        state: _UploadState,
        attempt: int,
        *,
        existing_drive_id: Optional[str] = None,
    ) -> TransferResult:
        """One attempt: fresh download URL, then a resumable Drive upload."""

        assert self.drive is not None
        # A fresh URL per attempt is what makes a flaky CDN node recoverable.
        metadata = self.quark.get_download_items([item.fid])[0]
        url = str(metadata.get("download_url") or "")
        size = int(metadata.get("size") or item.size or 0)
        name = str(metadata.get("file_name") or metadata.get("name") or item.name)
        if not url or size <= 0:
            raise TransferError(f"Invalid Quark download metadata for {name!r}")

        resumable = state.session_url is not None and state.session_size == size
        if resumable:
            try:
                state.uploaded = self.drive.uploaded_bytes(state.session_url, size)
            except DriveError as exc:
                self.emit(
                    "drive_session_reset",
                    name=name,
                    reason=redact_text(exc),
                )
                resumable = False
        if not resumable:
            state.session_url = None
            state.session_size = None
            state.uploaded = 0
            if existing_drive_id:
                mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                state.session_url = self.drive.initiate_update(
                    existing_drive_id,
                    size,
                    mime_type,
                )
                self.emit(
                    "updating_existing",
                    name=name,
                    size=size,
                    drive_id=existing_drive_id,
                )
            else:
                chosen, existing = self.drive.choose_destination(
                    parent_id,
                    name,
                    size,
                    on_exists=self.duplicate_policy,
                )
                if chosen is None:
                    self.emit(
                        "skipped_existing",
                        name=name,
                        size=size,
                        drive_id=existing.get("id") if existing else None,
                    )
                    return TransferResult(
                        status="skipped",
                        name=name,
                        source_size=size,
                        source_path=item.path,
                        drive_id=existing.get("id") if existing else None,
                        drive_name=name,
                    )
                mime_type = mimetypes.guess_type(chosen)[0] or "application/octet-stream"
                state.session_url = self.drive.initiate_upload(
                    chosen,
                    size,
                    parent_id,
                    mime_type,
                )
            state.session_size = size

        supports_range = self.quark.probe_range(url, size)
        self.emit(
            "source_probe",
            name=name,
            size=size,
            range_supported=supports_range,
            attempt=attempt,
            resume_from=state.uploaded,
        )

        if supports_range:
            final = self._upload_range(
                state.session_url,
                url,
                size,
                name,
                start=state.uploaded,
            )
        else:
            final = self._upload_stream(
                state.session_url,
                url,
                size,
                name,
                start=state.uploaded,
            )

        drive_id = str(final.get("id") or "")
        if not drive_id:
            raise TransferError("Drive did not return a file id after upload")
        verified = self.drive.get_file(drive_id)
        if int(verified.get("size") or -1) != size:
            raise TransferError(
                f"Drive byte verification failed: source={size}, drive={verified.get('size')}"
            )
        self.emit(
            "verified",
            name=verified.get("name"),
            drive_id=drive_id,
            size=size,
        )
        return TransferResult(
            status="ok",
            name=name,
            source_size=size,
            source_path=item.path,
            drive_id=drive_id,
            drive_name=str(verified.get("name") or name),
        )

    def _upload_range(
        self,
        session_url: str,
        source_url: str,
        size: int,
        name: str,
        *,
        start: int = 0,
    ) -> dict[str, Any]:
        """Upload by Range, downloading several chunks ahead of the upload.

        Drive requires resumable chunks strictly in order, so only the *fetch*
        side is parallelized: a bounded window of chunks is pulled from the CDN
        while the upload consumes them one by one. A single cross-border
        connection to the Quark CDN is the throughput ceiling, and this is what
        lifts it for the few very large files.
        """

        assert self.drive is not None
        depth = self._prefetch_depth(size)
        if depth <= 1:
            return self._upload_range_serial(
                session_url,
                source_url,
                size,
                name,
                start=start,
            )

        uploaded = start
        final: Optional[dict[str, Any]] = None
        pool = ThreadPoolExecutor(max_workers=depth, thread_name_prefix="quark-range")
        window: dict[int, Future] = {}
        next_offset = start
        try:
            while uploaded < size:
                while len(window) < depth and next_offset < size:
                    end = min(next_offset + self.chunk_size, size) - 1
                    window[next_offset] = pool.submit(
                        self.quark.fetch_range,
                        source_url,
                        next_offset,
                        end,
                    )
                    next_offset = end + 1
                future = window.pop(uploaded)
                try:
                    data = future.result()
                except BaseException:
                    for outstanding in window.values():
                        outstanding.cancel()
                    raise
                response = self.drive.put_chunk(
                    session_url,
                    start=uploaded,
                    total=size,
                    data=data,
                )
                uploaded += len(data)
                self.emit(
                    "progress",
                    name=name,
                    uploaded=uploaded,
                    total=size,
                    percent=round(uploaded * 100 / size, 2),
                )
                if response.status_code in (200, 201):
                    final = dict(response.json())
                    break
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        if not final or uploaded != size:
            raise TransferError("Drive did not finalize the ranged upload")
        return final

    def _upload_range_serial(
        self,
        session_url: str,
        source_url: str,
        size: int,
        name: str,
        *,
        start: int = 0,
    ) -> dict[str, Any]:
        assert self.drive is not None
        uploaded = start
        final: Optional[dict[str, Any]] = None
        while uploaded < size:
            end = min(uploaded + self.chunk_size, size) - 1
            data = self.quark.fetch_range(source_url, uploaded, end)
            response = self.drive.put_chunk(
                session_url,
                start=uploaded,
                total=size,
                data=data,
            )
            uploaded += len(data)
            self.emit(
                "progress",
                name=name,
                uploaded=uploaded,
                total=size,
                percent=round(uploaded * 100 / size, 2),
            )
            if response.status_code in (200, 201):
                final = dict(response.json())
                break
        if not final or uploaded != size:
            raise TransferError("Drive did not finalize the ranged upload")
        return final

    def _upload_stream(
        self,
        session_url: str,
        source_url: str,
        size: int,
        name: str,
        *,
        start: int = 0,
    ) -> dict[str, Any]:
        assert self.drive is not None
        uploaded = start
        final: Optional[dict[str, Any]] = None
        with self.quark.open_stream(source_url) as response:
            chunks = response.iter_content(1024 * 1024)
            leftover = self._skip_stream_bytes(chunks, start) if start else b""
            buffer = bytearray(leftover)
            for raw in chunks:
                if raw:
                    buffer.extend(raw)
                uploaded, final = self._drain_stream_buffer(
                    session_url,
                    size,
                    name,
                    buffer,
                    uploaded,
                )
                if final:
                    break
            if final is None:
                # Flush a tail that a resume skip already buffered, or a final
                # short chunk that arrived with the last read.
                uploaded, final = self._drain_stream_buffer(
                    session_url,
                    size,
                    name,
                    buffer,
                    uploaded,
                )
        if uploaded != size:
            # A truncated continuous response is exactly the CDN flakiness the
            # file-level retry exists for; the next attempt resumes at the
            # committed Drive offset.
            raise QuarkCdnError(
                f"Quark stream ended early: expected {size}, uploaded {uploaded}"
            )
        if not final:
            raise TransferError("Drive did not finalize the streamed upload")
        return final

    def _drain_stream_buffer(
        self,
        session_url: str,
        size: int,
        name: str,
        buffer: bytearray,
        uploaded: int,
    ) -> tuple[int, Optional[dict[str, Any]]]:
        """Send every complete chunk currently buffered."""

        assert self.drive is not None
        final: Optional[dict[str, Any]] = None
        while len(buffer) >= self.chunk_size or (
            uploaded + len(buffer) == size and buffer
        ):
            take = min(self.chunk_size, len(buffer), size - uploaded)
            if take <= 0:
                break
            data = bytes(buffer[:take])
            del buffer[:take]
            chunk = self.drive.put_chunk(
                session_url,
                start=uploaded,
                total=size,
                data=data,
            )
            uploaded += len(data)
            self.emit(
                "progress",
                name=name,
                uploaded=uploaded,
                total=size,
                percent=round(uploaded * 100 / size, 2),
            )
            if chunk.status_code in (200, 201):
                final = dict(chunk.json())
                break
        return uploaded, final

    @staticmethod
    def _skip_stream_bytes(chunks: Any, count: int) -> bytes:
        """Discard ``count`` already-uploaded bytes from a resumed stream.

        Returns any bytes read past the boundary so they are not lost.
        """

        remaining = count
        leftover = b""
        while remaining > 0:
            try:
                raw = next(chunks)
            except StopIteration:
                raise QuarkCdnError(
                    f"Quark stream ended while resuming at {count} bytes"
                ) from None
            if not raw:
                continue
            if len(raw) > remaining:
                leftover = raw[remaining:]
                remaining = 0
            else:
                remaining -= len(raw)
        return leftover
