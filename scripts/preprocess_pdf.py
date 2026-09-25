from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

from quark_cloud_transfer.gdrive import GoogleDriveClient


MIB = 1024 * 1024


def emit(event: str, **payload: object) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def parse_oauth(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("GDRIVE_OAUTH_JSON is not valid JSON") from exc
    if "installed" in data or "web" in data:
        data = data.get("installed") or data.get("web")
    if not isinstance(data, dict):
        raise SystemExit("GDRIVE_OAUTH_JSON must contain an object")
    missing = [
        key
        for key in ("client_id", "client_secret", "refresh_token")
        if not data.get(key)
    ]
    if missing:
        raise SystemExit("GDRIVE_OAUTH_JSON is missing: " + ", ".join(missing))
    return data


def decide_preprocess(
    *,
    size_bytes: int,
    mode: str,
    force_reason: str,
    max_direct_mib: float,
) -> tuple[bool, str]:
    if mode == "never":
        return False, "MODE_NEVER"
    if mode == "always":
        return True, "MODE_ALWAYS"
    reason = force_reason.strip()
    if reason:
        return True, f"FORCED_BY_UPSTREAM:{reason}"
    direct_limit = int(max_direct_mib * MIB)
    if size_bytes > direct_limit:
        return True, "SIZE_ABOVE_DIRECT_GEMINI_LIMIT"
    return False, "DIRECT_GEMINI_PREFERRED"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 * MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def page_text_stats(reader: PdfReader) -> tuple[list[int], list[int], int]:
    text_chars: list[int] = []
    image_like: list[int] = []
    total_chars = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        count = len(text.strip())
        text_chars.append(count)
        total_chars += count
        if count < 20:
            image_like.append(index + 1)
    return text_chars, image_like, total_chars


def ranges(values: list[int]) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        out.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    out.append(str(start) if start == previous else f"{start}-{previous}")
    return out


def choose_pages_per_chunk(
    *,
    size_bytes: int,
    page_count: int,
    target_chunk_mib: float,
    max_pages_per_chunk: int,
) -> int:
    if page_count < 1:
        raise ValueError("PDF has no pages")
    average_page_bytes = max(1.0, size_bytes / page_count)
    target_bytes = max(1, int(target_chunk_mib * MIB))
    by_size = max(1, int((target_bytes * 0.82) / average_page_bytes))
    return max(1, min(max_pages_per_chunk, by_size))


def write_pdf_range(
    reader: PdfReader,
    start_index: int,
    end_index: int,
    output_path: Path,
) -> None:
    writer = PdfWriter()
    for page_index in range(start_index, end_index):
        writer.add_page(reader.pages[page_index])
    with output_path.open("wb") as fh:
        writer.write(fh)


def make_segments(
    reader: PdfReader,
    *,
    source_path: Path,
    workdir: Path,
    target_chunk_mib: float,
    max_pages_per_chunk: int,
) -> list[dict[str, Any]]:
    page_count = len(reader.pages)
    pages_per_chunk = choose_pages_per_chunk(
        size_bytes=source_path.stat().st_size,
        page_count=page_count,
        target_chunk_mib=target_chunk_mib,
        max_pages_per_chunk=max_pages_per_chunk,
    )
    target_bytes = int(target_chunk_mib * MIB)
    segments: list[dict[str, Any]] = []
    start = 0
    sequence = 1

    while start < page_count:
        end = min(page_count, start + pages_per_chunk)
        while True:
            filename = f"segment_{sequence:03d}_p{start + 1:03d}-{end:03d}.pdf"
            path = workdir / filename
            write_pdf_range(reader, start, end, path)
            actual_size = path.stat().st_size
            if actual_size <= target_bytes or end - start <= 1:
                break
            path.unlink(missing_ok=True)
            span = max(1, math.ceil((end - start) / 2))
            end = start + span

        segments.append(
            {
                "sequence": sequence,
                "pdf_start": start + 1,
                "pdf_end": end,
                "page_count": end - start,
                "filename": filename,
                "local_path": str(path),
                "size_bytes": actual_size,
            }
        )
        sequence += 1
        start = end

    return segments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Conditionally preprocess a Google Drive PDF for Gemini. "
            "Small/normal PDFs are left untouched by default."
        )
    )
    parser.add_argument("--drive-file-id", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--source-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--mode", choices=("auto", "always", "never"), default="auto")
    parser.add_argument(
        "--force-reason",
        default="",
        help="Non-empty upstream failure/partial-read reason that forces preprocessing.",
    )
    parser.add_argument("--max-direct-mib", type=float, default=95.0)
    parser.add_argument("--target-chunk-mib", type=float, default=70.0)
    parser.add_argument("--max-pages-per-chunk", type=int, default=60)
    parser.add_argument(
        "--duplicate-policy",
        choices=("skip", "rename"),
        default="skip",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_direct_mib <= 0:
        raise SystemExit("max-direct-mib must be positive")
    if args.target_chunk_mib <= 0:
        raise SystemExit("target-chunk-mib must be positive")
    if args.max_pages_per_chunk < 1:
        raise SystemExit("max-pages-per-chunk must be >= 1")

    oauth_raw = (os.environ.get("GDRIVE_OAUTH_JSON") or "").strip()
    if not oauth_raw:
        raise SystemExit("Missing GDRIVE_OAUTH_JSON")
    drive = GoogleDriveClient(parse_oauth(oauth_raw))
    root_id = (os.environ.get("GDRIVE_ROOT_FOLDER_ID") or "").strip() or "root"

    metadata = drive.get_file(args.drive_file_id)
    name = str(metadata.get("name") or args.drive_file_id)
    mime_type = str(metadata.get("mimeType") or "")
    try:
        size_bytes = int(metadata.get("size") or 0)
    except (TypeError, ValueError):
        size_bytes = 0

    if mime_type != "application/pdf" and not name.lower().endswith(".pdf"):
        emit(
            "preprocess_decision",
            decision="NO_PREPROCESS_NEEDED",
            reason="NOT_A_PDF",
            drive_file_id=args.drive_file_id,
            name=name,
            size_bytes=size_bytes,
        )
        return 0

    should_preprocess, reason = decide_preprocess(
        size_bytes=size_bytes,
        mode=args.mode,
        force_reason=args.force_reason,
        max_direct_mib=args.max_direct_mib,
    )
    if not should_preprocess:
        emit(
            "preprocess_decision",
            decision="NO_PREPROCESS_NEEDED",
            reason=reason,
            drive_file_id=args.drive_file_id,
            name=name,
            size_bytes=size_bytes,
            max_direct_mib=args.max_direct_mib,
            next_action="Send original PDF directly to Gemini/Spark.",
        )
        return 0

    emit(
        "preprocess_decision",
        decision="PREPROCESS_REQUIRED",
        reason=reason,
        drive_file_id=args.drive_file_id,
        name=name,
        size_bytes=size_bytes,
    )

    with tempfile.TemporaryDirectory(prefix="kaoyan-pdf-") as tmp:
        workdir = Path(tmp)
        source_path = workdir / "source.pdf"
        downloaded = drive.download_to_path(args.drive_file_id, str(source_path))
        if size_bytes and downloaded != size_bytes:
            raise RuntimeError(
                f"Drive download byte mismatch: metadata={size_bytes}, downloaded={downloaded}"
            )

        source_sha256 = sha256_file(source_path)
        reader = PdfReader(str(source_path))
        page_count = len(reader.pages)
        if page_count < 1:
            raise RuntimeError("PDF contains no pages")

        text_chars, image_like_pages, total_text_chars = page_text_stats(reader)
        segments = make_segments(
            reader,
            source_path=source_path,
            workdir=workdir,
            target_chunk_mib=args.target_chunk_mib,
            max_pages_per_chunk=args.max_pages_per_chunk,
        )

        destination_id = drive.ensure_folder_path(args.destination, root_id=root_id)
        uploaded_segments: list[dict[str, Any]] = []
        for segment in segments:
            uploaded = drive.upload_path(
                segment["local_path"],
                destination_id,
                name=segment["filename"],
                mime_type="application/pdf",
                on_exists=args.duplicate_policy,
            )
            uploaded_segments.append(
                {
                    key: value
                    for key, value in segment.items()
                    if key != "local_path"
                }
                | {
                    "drive_file_id": uploaded.get("id"),
                    "drive_name": uploaded.get("name"),
                }
            )

        manifest = {
            "manifest_version": "1.0",
            "source_id": args.source_id,
            "job_id": args.job_id,
            "original_drive_file_id": args.drive_file_id,
            "original_name": name,
            "original_size_bytes": downloaded,
            "source_sha256": source_sha256,
            "decision": "PREPROCESS_REQUIRED",
            "decision_reason": reason,
            "page_count": page_count,
            "embedded_text_chars": total_text_chars,
            "image_like_page_count": len(image_like_pages),
            "image_like_page_ranges": ranges(image_like_pages),
            "text_rich_page_count": sum(1 for count in text_chars if count >= 20),
            "segment_count": len(uploaded_segments),
            "target_chunk_mib": args.target_chunk_mib,
            "max_pages_per_chunk": args.max_pages_per_chunk,
            "segments": uploaded_segments,
            "next_action": (
                "Gemini/Spark should read the segment PDFs directly. "
                "Use visual/formula review where embedded text is weak. "
                "Do not OCR the entire book again unless a bounded segment requires it."
            ),
        }
        manifest_path = workdir / "preprocess_manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        uploaded_manifest = drive.upload_path(
            str(manifest_path),
            destination_id,
            name="preprocess_manifest.json",
            mime_type="application/json",
            on_exists=args.duplicate_policy,
        )

        emit(
            "preprocess_complete",
            source_id=args.source_id,
            job_id=args.job_id,
            drive_file_id=args.drive_file_id,
            page_count=page_count,
            segment_count=len(uploaded_segments),
            image_like_page_count=len(image_like_pages),
            image_like_page_ranges=ranges(image_like_pages),
            manifest_drive_file_id=uploaded_manifest.get("id"),
            destination=args.destination,
            next_action="READY_FOR_GEMINI_DISTILL",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
