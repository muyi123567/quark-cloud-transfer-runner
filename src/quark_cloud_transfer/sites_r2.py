from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import requests

from .errors import TransferError
from .models import QuarkItem
from .storage import StoredObject

PART_SIZE = 1024 * 1024
READ_SIZE = 128 * 1024


class SitesR2:
    """Client for the owner-scoped Sites R2 tools over their MCP HTTP endpoint."""

    def __init__(self, endpoint: str, token: str) -> None:
        url = urlsplit(endpoint)
        local = url.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            (url.scheme != "https" and not (local and url.scheme == "http"))
            or not url.hostname or url.username or url.password or url.fragment or url.query
        ):
            raise TransferError("Supply the exact HTTPS Sites MCP endpoint")
        if not token:
            raise TransferError("An owner-authorized Sites MCP bearer token is required")
        self.endpoint = endpoint
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }

    def _call(self, operation: str, **arguments: Any) -> dict[str, Any]:
        call_id = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": f"r2_api_{operation}", "arguments": arguments},
        }
        for attempt in range(3):
            try:
                with requests.post(
                    self.endpoint, json=request, headers=self._headers,
                    allow_redirects=False, timeout=(10, 90),
                ) as response:
                    if response.status_code in (408, 429, 500, 502, 503, 504):
                        raise requests.ConnectionError(f"Sites HTTP {response.status_code}")
                    if response.status_code != 200:
                        raise TransferError(f"Sites {operation}: HTTP {response.status_code}")
                    return self._decode(response, call_id, operation)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == 2:
                    raise requests.ConnectionError(
                        f"Sites {operation}: transient HTTP failure after three attempts"
                    ) from None
                time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _decode(response: requests.Response, call_id: str, operation: str) -> dict[str, Any]:
        try:
            if response.headers.get("Content-Type", "").startswith("text/event-stream"):
                events = response.text.replace("\r\n", "\n").split("\n\n")
                messages = (
                    json.loads("\n".join(line[5:].lstrip() for line in event.splitlines()
                                         if line.startswith("data:")))
                    for event in events if any(line.startswith("data:") for line in event.splitlines())
                )
                message = next(msg for msg in messages if msg.get("id") == call_id)
            else:
                message = response.json()
            if message.get("id") != call_id or "error" in message:
                raise TransferError(f"Sites {operation}: MCP request rejected")
            result = message["result"]
            if result.get("isError"):
                raise TransferError(f"Sites {operation}: owner tool rejected the request")
            payload = json.loads(next(block["text"] for block in result["content"]
                                      if block["type"] == "text"))
            if isinstance(payload, dict):
                return payload
        except (ValueError, KeyError, StopIteration, TypeError, AttributeError):
            pass
        raise TransferError(f"Sites {operation}: invalid MCP response")

    def upload(self, item: QuarkItem, destination: str, source: BinaryIO) -> StoredObject:
        source.seek(0)
        whole = hashlib.sha256()
        hashes = []
        size = 0
        while block := source.read(PART_SIZE):
            whole.update(block)
            hashes.append(hashlib.sha256(block).hexdigest())
            size += len(block)
        if size != item.size or not size:
            raise TransferError("Sites upload source length differs from the Quark selection")
        digest = whole.hexdigest()
        identity = json.dumps([destination, item.fid, item.name, digest], ensure_ascii=False)
        session = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        init = self._call(
            "init", id=session, fileName=item.name, size=size, sha256=digest,
            partHashes=hashes, mime=mimetypes.guess_type(item.name)[0] or "application/octet-stream",
        )
        key = init.get("key")
        if (
            init.get("id") != session or init.get("partCount") != len(hashes)
            or init.get("partSize") != PART_SIZE or not isinstance(key, str) or not key
            or init.get("status") not in {"open", "commit"}
        ):
            raise TransferError("Sites returned incompatible upload session metadata")
        if init["status"] == "open":
            present = self._present_parts(session, len(hashes))
            for number in range(1, len(hashes) + 1):
                if number in present:
                    continue
                source.seek((number - 1) * PART_SIZE)
                block = source.read(PART_SIZE)
                if hashlib.sha256(block).hexdigest() != hashes[number - 1]:
                    raise TransferError("Staged source changed after hashing")
                receipt = self._call(
                    "part", id=session, number=number, base64=base64.b64encode(block).decode("ascii"),
                )
                if receipt.get("id") != session or receipt.get("partNumber") != number:
                    raise TransferError("Sites returned a mismatched part receipt")
            receipt = self._call("commit", id=session)
            if receipt.get("id") != session or receipt.get("status") not in {"committed", "already-committed"}:
                raise TransferError("Sites did not confirm upload commit")
        _, remote_size, remote_hash = self._read(key, 0)
        if (remote_size, remote_hash) != (size, digest):
            raise TransferError("Sites committed object failed identity verification")
        return StoredObject(key, size, digest)

    def _present_parts(self, session: str, count: int) -> set[int]:
        start = 0
        present: set[int] = set()
        while True:
            page = self._call("status", id=session, start=start)
            parts = page.get("present")
            if page.get("id") != session or not isinstance(parts, list) or any(
                type(part) is not int or not 1 <= part <= count for part in parts
            ):
                raise TransferError("Sites returned invalid upload status")
            present.update(parts)
            following = page.get("next")
            if following is None:
                return present
            if type(following) is not int or not start < following <= count:
                raise TransferError("Sites returned invalid status cursor")
            start = following

    def _read(self, key: str, offset: int, *, legacy: bool = False) -> tuple[bytes, int, str]:
        operation = "legacy_download" if legacy else "download"
        page = self._call(operation, key=key, offset=offset)
        try:
            block = base64.b64decode(page["base64"], validate=True)
            size, digest = page["size"], page["sha256"]
        except (KeyError, TypeError, ValueError):
            raise TransferError("Sites returned malformed download data") from None
        end = offset + len(block)
        if (
            type(size) is not int or not isinstance(digest, str)
            or not re.fullmatch("[0-9a-f]{64}", digest)
            or page.get("key") != key or page.get("offset") != offset
            or page.get("length") != len(block) or not 0 < len(block) <= READ_SIZE
            or end > size or page.get("next") != (end if end < size else None)
        ):
            raise TransferError("Sites returned an invalid download range")
        return block, size, digest

    def download(self, key: str, output: Path, *, legacy: bool = False) -> StoredObject:
        import fcntl

        if output.exists() or output.is_symlink():
            raise TransferError("Download output already exists")
        first, size, digest = self._read(key, 0, legacy=legacy)
        identity = hashlib.sha256(json.dumps([self.endpoint, key, digest, legacy]).encode()).hexdigest()
        partial = output.with_name(f".{output.name}.{identity}.part")
        fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransferError("A download for this output is already running") from None
            whole = hashlib.sha256()
            offset = 0
            while saved := stream.read(READ_SIZE):
                whole.update(saved)
                offset += len(saved)
            while offset < size:
                if offset:
                    block, remote_size, remote_hash = self._read(key, offset, legacy=legacy)
                    if (remote_size, remote_hash) != (size, digest):
                        raise TransferError("Sites object changed during download")
                else:
                    block = first
                stream.write(block)
                stream.flush()
                whole.update(block)
                offset += len(block)
            if offset != size or whole.hexdigest() != digest:
                raise TransferError(f"Download checksum mismatch in partial file {partial.name}")
            os.fsync(stream.fileno())
            os.link(partial, output)
            partial.unlink()
        return StoredObject(key, size, digest)

    def list(self, cursor: str | None = None) -> dict[str, Any]:
        return self._call("list", **({"cursor": cursor} if cursor is not None else {}))

    def legacy_inventory(self, start: int = 0) -> dict[str, Any]:
        return self._call("legacy_inventory", start=start)
