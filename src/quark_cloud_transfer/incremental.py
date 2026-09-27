from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

from .models import QuarkItem
from .transfer import TransferService


_RETRANSFER_TOP_RE = re.compile(r"^(\d+)\((\d+)\)(.*)$")


@dataclass(frozen=True)
class DriveInventoryItem:
    path: str
    file_id: str
    size: int
    md5: Optional[str] = None


@dataclass(frozen=True)
class IncrementalItem:
    status: str
    logical_path: str
    source: Optional[QuarkItem] = None
    drive: Optional[DriveInventoryItem] = None
    reason: str = ""


def _relative_source_path(item: QuarkItem, source_path: str) -> str:
    root_name = source_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    marker = f"/{root_name}/"
    raw = item.path.replace("\\", "/")
    if marker in raw:
        return raw.split(marker, 1)[1].lstrip("/")
    return raw.lstrip("/")


def _top_component(path: str) -> str:
    return path.split("/", 1)[0]


def _rest_after_top(path: str) -> str:
    return path.split("/", 1)[1] if "/" in path else ""


def _candidate_canonical_top(name: str) -> Optional[str]:
    match = _RETRANSFER_TOP_RE.match(name)
    if not match:
        return None
    return f"{match.group(1)}{match.group(3)}"


def _branch_signature(entries: Iterable[tuple[str, QuarkItem]], top: str) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for rel, item in entries:
        if _top_component(rel) != top:
            continue
        rest = _rest_after_top(rel)
        if rest:
            out.add((rest, int(item.size)))
    return out


def normalize_retransfer_branches(
    items: list[QuarkItem],
    *,
    source_path: str,
    min_overlap_ratio: float = 0.75,
    min_overlap_files: int = 3,
) -> tuple[dict[str, list[QuarkItem]], dict[str, str]]:
    """Map Quark '(1)' re-transfer branches back to their logical sibling.

    A name pattern alone is never enough.  The candidate branch is collapsed
    only when the unsuffixed sibling exists and the two subtrees have strong
    relative-path+size overlap.
    """

    rel_items = [(_relative_source_path(item, source_path), item) for item in items]
    tops = {_top_component(rel) for rel, _ in rel_items if rel}
    aliases: dict[str, str] = {}

    for top in sorted(tops):
        canonical = _candidate_canonical_top(top)
        if not canonical or canonical not in tops:
            continue
        left = _branch_signature(rel_items, top)
        right = _branch_signature(rel_items, canonical)
        if not left or not right:
            continue
        overlap = len(left & right)
        denominator = min(len(left), len(right))
        ratio = overlap / denominator if denominator else 0.0
        required = min(min_overlap_files, denominator)
        if overlap >= required and ratio >= min_overlap_ratio:
            aliases[top] = canonical

    logical: dict[str, list[QuarkItem]] = {}
    for rel, item in rel_items:
        if not rel:
            continue
        top = _top_component(rel)
        mapped_top = aliases.get(top, top)
        rest = _rest_after_top(rel)
        mapped = mapped_top if not rest else f"{mapped_top}/{rest}"
        logical.setdefault(mapped, []).append(item)

    return logical, aliases


def _pick_source(candidates: list[QuarkItem]) -> tuple[Optional[QuarkItem], str]:
    if len(candidates) == 1:
        return candidates[0], ""
    sizes = {int(item.size) for item in candidates}
    ordered = sorted(
        candidates,
        key=lambda item: (
            int(item.updated_at or 0),
            item.path,
        ),
        reverse=True,
    )
    if len(sizes) == 1:
        return ordered[0], "duplicate re-transfer copies collapsed"
    newest = int(ordered[0].updated_at or 0)
    second = int(ordered[1].updated_at or 0)
    if newest > second:
        return ordered[0], "newer re-transfer copy selected"
    return None, "multiple logical copies have different sizes and no unique newest version"


def build_incremental_plan(
    items: list[QuarkItem],
    *,
    source_path: str,
    drive_items: list[DriveInventoryItem],
) -> tuple[list[IncrementalItem], dict[str, str]]:
    logical, aliases = normalize_retransfer_branches(items, source_path=source_path)
    drive_by_path = {item.path: item for item in drive_items}
    plan: list[IncrementalItem] = []
    seen_paths: set[str] = set()

    for path in sorted(logical):
        selected, note = _pick_source(logical[path])
        seen_paths.add(path)
        if selected is None:
            plan.append(
                IncrementalItem(
                    status="conflict",
                    logical_path=path,
                    reason=note,
                )
            )
            continue
        existing = drive_by_path.get(path)
        if existing is None:
            plan.append(
                IncrementalItem(
                    status="new",
                    logical_path=path,
                    source=selected,
                    reason=note,
                )
            )
            continue

        source_hash = (selected.content_hash or "").lower() or None
        drive_hash = (existing.md5 or "").lower() or None
        same_size = int(selected.size) == int(existing.size)
        if same_size and (not source_hash or not drive_hash or source_hash == drive_hash):
            plan.append(
                IncrementalItem(
                    status="unchanged",
                    logical_path=path,
                    source=selected,
                    drive=existing,
                    reason=note or ("same size and hash" if source_hash and drive_hash else "same size"),
                )
            )
        else:
            reason = "size changed"
            if same_size and source_hash and drive_hash and source_hash != drive_hash:
                reason = "content hash changed"
            plan.append(
                IncrementalItem(
                    status="modified",
                    logical_path=path,
                    source=selected,
                    drive=existing,
                    reason=reason,
                )
            )

    for path, existing in sorted(drive_by_path.items()):
        if path not in seen_paths:
            plan.append(
                IncrementalItem(
                    status="missing",
                    logical_path=path,
                    drive=existing,
                    reason="present in Drive baseline but absent from current logical source",
                )
            )

    return plan, aliases


class IncrementalSyncService:
    def __init__(self, transfer: TransferService) -> None:
        if transfer.drive is None:
            raise ValueError("Incremental sync requires a Google Drive client")
        self.transfer = transfer
        self.drive = transfer.drive

    def plan(
        self,
        *,
        source_path: str,
        destination: str,
        root_fid: str = "0",
    ) -> tuple[list[IncrementalItem], dict[str, str], str]:
        source_items = self.transfer.select_items(
            source_path=source_path,
            root_fid=root_fid,
        )
        destination_id = self.drive.resolve_folder_path(
            destination,
            root_id=self.transfer.drive_root_id,
        )
        drive_rows = [
            DriveInventoryItem(
                path=str(row["path"]),
                file_id=str(row["id"]),
                size=int(row.get("size") or 0),
                md5=(str(row.get("md5Checksum")) if row.get("md5Checksum") else None),
            )
            for row in self.drive.walk_files(destination_id)
        ]
        plan, aliases = build_incremental_plan(
            source_items,
            source_path=source_path,
            drive_items=drive_rows,
        )
        return plan, aliases, destination_id

    def run(
        self,
        *,
        source_path: str,
        destination: str,
        dry_run: bool,
        root_fid: str = "0",
    ) -> list[IncrementalItem]:
        plan, aliases, destination_id = self.plan(
            source_path=source_path,
            destination=destination,
            root_fid=root_fid,
        )
        counts: dict[str, int] = {}
        for item in plan:
            counts[item.status] = counts.get(item.status, 0) + 1
        self.transfer.emit(
            "incremental_plan",
            dry_run=dry_run,
            destination=destination,
            aliases=aliases,
            counts=counts,
            transfer_files=sum(
                1 for item in plan if item.status in {"new", "modified"}
            ),
            transfer_bytes=sum(
                int(item.source.size)
                for item in plan
                if item.status in {"new", "modified"} and item.source is not None
            ),
            items=[
                {
                    "status": item.status,
                    "logical_path": item.logical_path,
                    "size": item.source.size if item.source else item.drive.size if item.drive else None,
                    "reason": item.reason,
                }
                for item in plan
            ],
        )
        if dry_run:
            return plan

        if any(item.status == "conflict" for item in plan):
            raise RuntimeError("Incremental sync contains conflicts; resolve them before writing")

        folder_cache: dict[str, str] = {"": destination_id}
        for entry in plan:
            if entry.status not in {"new", "modified"} or entry.source is None:
                continue
            parent_rel = str(PurePosixPath(entry.logical_path).parent)
            if parent_rel == ".":
                parent_rel = ""
            if parent_rel in folder_cache:
                parent_id = folder_cache[parent_rel]
            else:
                parent_id = self.drive.ensure_folder_path(parent_rel, root_id=destination_id)
                folder_cache[parent_rel] = parent_id

            existing_id = entry.drive.file_id if entry.status == "modified" and entry.drive else None
            result = self.transfer._transfer_one(
                entry.source,
                parent_id,
                existing_drive_id=existing_id,
            )
            if result.status not in {"ok", "skipped"}:
                raise RuntimeError(
                    f"Incremental transfer failed for {entry.logical_path}: {result.error}"
                )

        self.transfer.emit(
            "incremental_complete",
            counts=counts,
            changed=sum(1 for item in plan if item.status in {"new", "modified"}),
        )
        return plan
