from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .errors import ConfigError

DEFAULT_CHUNK_MIB = 16
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_NODES = 100_000


@dataclass(frozen=True)
class Settings:
    quark_cookie: str
    gdrive_oauth: Optional[dict[str, Any]]
    gdrive_root_folder_id: str
    chunk_size: int
    max_files: int
    max_depth: int
    max_nodes: int
    duplicate_policy: str


def _decode_b64_env(value: str, name: str) -> str:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except Exception as exc:
        raise ConfigError(f"{name} is not valid base64 text") from exc


def _read_secret(
    env: Mapping[str, str],
    raw_name: str,
    b64_name: str,
    *,
    required: bool,
) -> Optional[str]:
    raw = (env.get(raw_name) or "").strip()
    encoded = (env.get(b64_name) or "").strip()
    if raw and encoded:
        raise ConfigError(f"Set only one of {raw_name} or {b64_name}")
    if encoded:
        raw = _decode_b64_env(encoded, b64_name).strip()
    if required and not raw:
        raise ConfigError(f"Missing required secret: {raw_name}")
    return raw or None


def _parse_oauth_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("GDRIVE_OAUTH_JSON is not valid JSON") from exc

    if "installed" in data or "web" in data:
        data = data.get("installed") or data.get("web")
    if not isinstance(data, dict):
        raise ConfigError("GDRIVE_OAUTH_JSON must contain an object")

    missing = [key for key in ("client_id", "client_secret", "refresh_token") if not data.get(key)]
    if missing:
        raise ConfigError("GDRIVE_OAUTH_JSON is missing: " + ", ".join(missing))
    return data


def _validate_chunk_size(chunk_size: int) -> None:
    if chunk_size <= 0:
        raise ConfigError("chunk_size must be positive")
    if chunk_size % (256 * 1024) != 0:
        raise ConfigError("chunk_size must be a multiple of 256 KiB")


def load_settings(
    *,
    require_drive: bool = True,
    chunk_mib: int = DEFAULT_CHUNK_MIB,
    max_files: int = DEFAULT_MAX_FILES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
    duplicate_policy: str = "skip",
    root_folder_id: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Settings:
    env = env or os.environ

    quark_cookie = _read_secret(
        env,
        "QUARK_COOKIE",
        "QUARK_COOKIE_B64",
        required=True,
    )
    assert quark_cookie is not None
    if "__puus=" not in quark_cookie:
        raise ConfigError("QUARK_COOKIE is missing __puus; run scripts/quark_login.py again")

    oauth_raw = _read_secret(
        env,
        "GDRIVE_OAUTH_JSON",
        "GDRIVE_OAUTH_JSON_B64",
        required=require_drive,
    )
    oauth = _parse_oauth_json(oauth_raw)

    chunk_size = chunk_mib * 1024 * 1024
    _validate_chunk_size(chunk_size)
    if max_files < 1:
        raise ConfigError("max_files must be at least 1")
    if max_depth < 0:
        raise ConfigError("max_depth cannot be negative")
    if max_nodes < 1:
        raise ConfigError("max_nodes must be at least 1")
    if duplicate_policy not in {"skip", "rename"}:
        raise ConfigError("duplicate_policy must be 'skip' or 'rename'")

    root_id = (
        root_folder_id
        or (env.get("GDRIVE_ROOT_FOLDER_ID") or "").strip()
        or "root"
    )
    return Settings(
        quark_cookie=quark_cookie,
        gdrive_oauth=oauth,
        gdrive_root_folder_id=root_id,
        chunk_size=chunk_size,
        max_files=max_files,
        max_depth=max_depth,
        max_nodes=max_nodes,
        duplicate_policy=duplicate_policy,
    )
