from __future__ import annotations

import argparse
import json
from typing import Optional

from .config import (
    DEFAULT_CHUNK_MIB,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_NODES,
    load_settings,
)
from .errors import QuarkCloudTransferError
from .gdrive import GoogleDriveClient
from .quark import QuarkClient
from .redaction import redact_text
from .transfer import TransferService


def emit(event: str, **payload: object) -> None:
    print(
        json.dumps({"event": event, **payload}, ensure_ascii=False),
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream Quark Drive files into Google Drive without full local staging."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--query", help="Case-insensitive Quark filename substring.")
    selection.add_argument("--source-path", help="Exact Quark path to a file or folder.")
    selection.add_argument("--source-fid", help="Exact 32-character Quark Web FID.")
    parser.add_argument("--destination", required=True, help="Google Drive folder path.")
    parser.add_argument("--drive-root-id", help="Override GDRIVE_ROOT_FOLDER_ID.")
    parser.add_argument("--quark-root-fid", default="0", help="Quark traversal root FID.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chunk-mib", type=int, default=DEFAULT_CHUNK_MIB)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    parser.add_argument(
        "--duplicate-policy",
        choices=("skip", "rename"),
        default="skip",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(
            require_drive=not args.dry_run,
            chunk_mib=args.chunk_mib,
            max_files=args.max_files,
            max_depth=args.max_depth,
            max_nodes=args.max_nodes,
            duplicate_policy=args.duplicate_policy,
            root_folder_id=args.drive_root_id,
        )
        quark = QuarkClient(settings.quark_cookie)
        drive = (
            None
            if args.dry_run
            else GoogleDriveClient(settings.gdrive_oauth or {})
        )
        service = TransferService(
            quark,
            drive,
            chunk_size=settings.chunk_size,
            max_files=settings.max_files,
            max_depth=settings.max_depth,
            max_nodes=settings.max_nodes,
            duplicate_policy=settings.duplicate_policy,
            drive_root_id=settings.gdrive_root_folder_id,
            emit=emit,
        )
        service.run(
            destination=args.destination,
            dry_run=args.dry_run,
            query=args.query,
            source_path=args.source_path,
            source_fid=args.source_fid,
            root_fid=args.quark_root_fid,
        )
        return 0
    except (QuarkCloudTransferError, ValueError) as exc:
        emit("error", message=redact_text(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
