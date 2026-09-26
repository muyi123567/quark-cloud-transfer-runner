from __future__ import annotations

import mimetypes
from collections.abc import Callable
from typing import Any, Optional

from .errors import TransferError
from .gdrive import GoogleDriveClient
from .models import QuarkItem, TransferResult
from .quark import QuarkClient

EventSink = Callable[..., None]


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
    ) -> None:
        self.quark = quark
        self.drive = drive
        self.chunk_size = chunk_size
        self.max_files = max_files
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.duplicate_policy = duplicate_policy
        self.drive_root_id = drive_root_id
        self.emit = emit

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

        parent_id = self.drive.ensure_folder_path(
            destination,
            root_id=self.drive_root_id,
        )
        self.emit("drive_folder_ready", destination=destination, folder_id=parent_id)

        results: list[TransferResult] = []
        folder_cache: dict[str, str] = {"": parent_id}
        source_root_path = ""
        if source_path:
            normalized_source = source_path.replace("\\", "/").rstrip("/")
            source_root_name = normalized_source.rsplit("/", 1)[-1]
            if source_root_name:
                source_root_path = f"/{source_root_name}"

        for item in items:
            item_parent_id = parent_id
            if source_root_path and item.path.startswith(source_root_path + "/"):
                relative_path = item.path[len(source_root_path) + 1 :]
                relative_parent = (
                    relative_path.rsplit("/", 1)[0]
                    if "/" in relative_path
                    else ""
                )
                if relative_parent:
                    item_parent_id = folder_cache.get(relative_parent, "")
                    if not item_parent_id:
                        item_parent_id = self.drive.ensure_folder_path(
                            relative_parent,
                            root_id=parent_id,
                        )
                        folder_cache[relative_parent] = item_parent_id
                        self.emit(
                            "drive_subfolder_ready",
                            source_relative_path=relative_parent,
                            folder_id=item_parent_id,
                        )
            results.append(self._transfer_one(item, item_parent_id))
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
        return results

    def _transfer_one(self, item: QuarkItem, parent_id: str) -> TransferResult:
        assert self.drive is not None
        metadata = self.quark.get_download_items([item.fid])[0]
        url = str(metadata.get("download_url") or "")
        size = int(metadata.get("size") or item.size or 0)
        name = str(metadata.get("file_name") or metadata.get("name") or item.name)
        if not url or size <= 0:
            raise TransferError(f"Invalid Quark download metadata for {name!r}")

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
        session_url = self.drive.initiate_upload(chosen, size, parent_id, mime_type)
        supports_range = self.quark.probe_range(url, size)
        self.emit(
            "source_probe",
            name=name,
            size=size,
            range_supported=supports_range,
        )

        if supports_range:
            final = self._upload_range(session_url, url, size, chosen)
        else:
            final = self._upload_stream(session_url, url, size, chosen)

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
            drive_name=str(verified.get("name") or chosen),
        )

    def _upload_range(
        self,
        session_url: str,
        source_url: str,
        size: int,
        name: str,
    ) -> dict[str, Any]:
        assert self.drive is not None
        uploaded = 0
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
    ) -> dict[str, Any]:
        assert self.drive is not None
        uploaded = 0
        final: Optional[dict[str, Any]] = None
        with self.quark.open_stream(source_url) as response:
            buffer = bytearray()
            for raw in response.iter_content(1024 * 1024):
                if raw:
                    buffer.extend(raw)
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
                if final:
                    break
        if uploaded != size:
            raise TransferError(
                f"Quark stream ended early: expected {size}, uploaded {uploaded}"
            )
        if not final:
            raise TransferError("Drive did not finalize the streamed upload")
        return final
