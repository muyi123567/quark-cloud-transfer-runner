from __future__ import annotations

from quark_cloud_transfer.incremental import (
    DriveInventoryItem,
    build_incremental_plan,
    normalize_retransfer_branches,
)
from quark_cloud_transfer.models import QuarkItem


def q(fid: str, path: str, size: int, updated: int = 0) -> QuarkItem:
    return QuarkItem(
        fid=fid,
        name=path.rsplit("/", 1)[-1],
        path=path,
        size=size,
        is_dir=False,
        updated_at=updated,
    )


def test_retransfer_branch_only_collapses_with_subtree_overlap() -> None:
    items = [
        q("a1", "/澄潇宇数学大观/1.高数/x/a.pdf", 10, 1),
        q("a2", "/澄潇宇数学大观/1.高数/x/b.pdf", 20, 1),
        q("a3", "/澄潇宇数学大观/1.高数/x/c.pdf", 30, 1),
        q("b1", "/澄潇宇数学大观/1(1).高数/x/a.pdf", 10, 2),
        q("b2", "/澄潇宇数学大观/1(1).高数/x/b.pdf", 20, 2),
        q("b3", "/澄潇宇数学大观/1(1).高数/x/c.pdf", 30, 2),
        q("b4", "/澄潇宇数学大观/1(1).高数/x/new.pdf", 40, 2),
    ]
    logical, aliases = normalize_retransfer_branches(
        items,
        source_path="/来自：分享/澄潇宇数学大观",
    )
    assert aliases == {"1(1).高数": "1.高数"}
    assert len(logical["1.高数/x/a.pdf"]) == 2
    assert len(logical["1.高数/x/new.pdf"]) == 1


def test_similar_name_without_overlap_is_not_collapsed() -> None:
    items = [
        q("a1", "/澄潇宇数学大观/1.高数/x/a.pdf", 10),
        q("b1", "/澄潇宇数学大观/1(1).高数/y/z.pdf", 99),
    ]
    logical, aliases = normalize_retransfer_branches(
        items,
        source_path="/澄潇宇数学大观",
    )
    assert aliases == {}
    assert "1(1).高数/y/z.pdf" in logical


def test_plan_only_transfers_new_and_modified() -> None:
    items = [
        q("old-a", "/澄潇宇数学大观/1.高数/a.pdf", 10, 1),
        q("old-b", "/澄潇宇数学大观/1.高数/b.pdf", 20, 1),
        q("old-c", "/澄潇宇数学大观/1.高数/c.pdf", 30, 1),
        q("new-a", "/澄潇宇数学大观/1(1).高数/a.pdf", 10, 2),
        q("new-b", "/澄潇宇数学大观/1(1).高数/b.pdf", 25, 2),
        q("new-c", "/澄潇宇数学大观/1(1).高数/c.pdf", 30, 2),
        q("new-d", "/澄潇宇数学大观/1(1).高数/d.pdf", 40, 2),
    ]
    drive = [
        DriveInventoryItem("1.高数/a.pdf", "da", 10),
        DriveInventoryItem("1.高数/b.pdf", "db", 20),
        DriveInventoryItem("1.高数/c.pdf", "dc", 30),
        DriveInventoryItem("1.高数/removed.pdf", "dr", 50),
    ]
    plan, aliases = build_incremental_plan(
        items,
        source_path="/澄潇宇数学大观",
        drive_items=drive,
    )
    by_path = {item.logical_path: item.status for item in plan}

    assert aliases == {"1(1).高数": "1.高数"}
    assert by_path["1.高数/a.pdf"] == "unchanged"
    assert by_path["1.高数/b.pdf"] == "modified"
    assert by_path["1.高数/c.pdf"] == "unchanged"
    assert by_path["1.高数/d.pdf"] == "new"
    assert by_path["1.高数/removed.pdf"] == "missing"


def test_different_size_duplicates_without_unique_newest_are_conflict() -> None:
    items = [
        q("a", "/澄潇宇数学大观/1.高数/x/a.pdf", 10, 0),
        q("b", "/澄潇宇数学大观/1(1).高数/x/a.pdf", 11, 0),
        q("a2", "/澄潇宇数学大观/1.高数/x/b.pdf", 20, 0),
        q("b2", "/澄潇宇数学大观/1(1).高数/x/b.pdf", 20, 0),
        q("a3", "/澄潇宇数学大观/1.高数/x/c.pdf", 30, 0),
        q("b3", "/澄潇宇数学大观/1(1).高数/x/c.pdf", 30, 0),
    ]
    plan, _ = build_incremental_plan(
        items,
        source_path="/澄潇宇数学大观",
        drive_items=[],
    )
    by_path = {item.logical_path: item.status for item in plan}
    assert by_path["1.高数/x/a.pdf"] == "conflict"


def test_retransfer_branch_can_match_drive_baseline_without_source_sibling() -> None:
    items = [
        q("b1", "/澄潇宇数学大观/1(1).高数/x/a.pdf", 10, 2),
        q("b2", "/澄潇宇数学大观/1(1).高数/x/b.pdf", 20, 2),
        q("b3", "/澄潇宇数学大观/1(1).高数/x/c.pdf", 30, 2),
    ]
    drive = [
        DriveInventoryItem("1.高数/x/a.pdf", "a", 10),
        DriveInventoryItem("1.高数/x/b.pdf", "b", 20),
        DriveInventoryItem("1.高数/x/c.pdf", "c", 30),
    ]
    plan, aliases = build_incremental_plan(
        items,
        source_path="/澄潇宇数学大观",
        drive_items=drive,
    )
    assert aliases == {"1(1).高数": "1.高数"}
    assert {item.status for item in plan} == {"unchanged"}


def test_generic_suffix_folder_matches_drive_baseline() -> None:
    items = [
        q("a", "/澄潇宇数学大观/老版文件（不推荐）(1)/x/a.pdf", 10, 2),
        q("b", "/澄潇宇数学大观/老版文件（不推荐）(1)/x/b.pdf", 20, 2),
    ]
    drive = [
        DriveInventoryItem("老版文件（不推荐）/x/a.pdf", "a", 10),
        DriveInventoryItem("老版文件（不推荐）/x/b.pdf", "b", 20),
    ]
    plan, aliases = build_incremental_plan(
        items,
        source_path="/澄潇宇数学大观",
        drive_items=drive,
    )
    assert aliases == {"老版文件（不推荐）(1)": "老版文件（不推荐）"}
    assert {item.status for item in plan} == {"unchanged"}


def test_duplicate_root_file_is_collapsed_by_same_size() -> None:
    items = [
        q("dup", "/澄潇宇数学大观/大观题库打印二维码(1).png", 85868, 2),
    ]
    drive = [
        DriveInventoryItem("大观题库打印二维码.png", "orig", 85868),
    ]
    plan, _ = build_incremental_plan(
        items,
        source_path="/澄潇宇数学大观",
        drive_items=drive,
    )
    assert [(item.logical_path, item.status) for item in plan] == [
        ("大观题库打印二维码.png", "unchanged")
    ]
