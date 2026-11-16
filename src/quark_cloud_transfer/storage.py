from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from .models import QuarkItem


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str


class StorageAdapter(Protocol):
    def upload(self, item: QuarkItem, destination: str, source: BinaryIO) -> StoredObject:
        """Upload a seekable source with its original Quark identity."""

    def download(self, key: str, output: Path) -> StoredObject:
        """Download and verify the object before publishing the output file."""
