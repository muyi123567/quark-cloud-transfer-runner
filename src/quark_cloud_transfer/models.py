from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class QuarkItem:
    fid: str
    name: str
    path: str
    size: int
    is_dir: bool
    updated_at: Optional[int] = None


@dataclass(frozen=True)
class TransferResult:
    status: str
    name: str
    source_size: int
    source_path: str
    drive_id: Optional[str] = None
    drive_name: Optional[str] = None
