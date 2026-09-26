from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Dict, Iterable, List, Optional

import requests

from .errors import QuarkError
from .models import QuarkItem

QUARK_BASE = "https://drive-pc.quark.cn/1/clouddrive"
QUARK_DOWNLOAD = "https://drive-pc.quark.cn/1/clouddrive/file/download"
QUARK_CONFIG = "https://drive-pc.quark.cn/1/clouddrive/config"
QUARK_COMMON_QUERY = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
QUARK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
    "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
)
_COOKIE_REFRESH_KEYS = ("__puus", "__pus")
_DOWNLOAD_REFRESH_SECONDS = 15 * 60


def _join_path(base: str, name: str) -> str:
    if not base:
        return f"/{name}"
    return f"{base.rstrip('/')}/{name}"


def _parse_cookie(cookie: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for raw in cookie.split(";"):
        part = raw.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            pairs[name] = value.strip()
    return pairs


def _format_cookie(pairs: dict[str, str], *, omit: set[str] | None = None) -> str:
    excluded = omit or set()
    return "; ".join(
        f"{name}={value}" for name, value in pairs.items() if name not in excluded
    )


class QuarkClient:
    def __init__(
        self,
        cookie: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 45,
    ) -> None:
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.trust_env = False
        self._cookies = _parse_cookie(cookie)
        self.cookie = _format_cookie(self._cookies)
        self._last_forced_refresh = 0.0
        self.session.headers.update(
            {
                "User-Agent": QUARK_UA,
                "Referer": "https://pan.quark.cn/",
                "Origin": "https://pan.quark.cn",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def _cookie_header(self, *, omit: set[str] | None = None) -> dict[str, str]:
        return {"Cookie": _format_cookie(self._cookies, omit=omit)}

    def _merge_cookie_updates(self, response: requests.Response) -> None:
        response_cookies = getattr(response, "cookies", None)
        if response_cookies is None:
            return
        changed = False
        for name in _COOKIE_REFRESH_KEYS:
            try:
                value = response_cookies.get(name)
            except Exception:
                value = None
            if value and self._cookies.get(name) != value:
                self._cookies[name] = str(value)
                changed = True
        if changed:
            self.cookie = _format_cookie(self._cookies)

    def _response_json(self, response: requests.Response, operation: str) -> Dict[str, Any]:
        self._merge_cookie_updates(response)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise QuarkError(f"{operation} failed with HTTP {response.status_code}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise QuarkError(f"{operation} returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise QuarkError(f"{operation} returned an unexpected payload")
        code = payload.get("code")
        if code not in (0, None):
            message = payload.get("message") or payload.get("msg") or "unknown Quark error"
            raise QuarkError(f"{operation} failed: code={code} message={message}")
        return payload

    def refresh_session(self) -> None:
        # __puus is short-lived. Omitting only that cookie asks Quark to rotate it
        # while retaining the long-lived login cookies. Keep the refreshed value
        # in memory only; never print or persist it.
        response = self.session.get(
            QUARK_CONFIG,
            params=QUARK_COMMON_QUERY,
            headers=self._cookie_header(omit={"__puus"}),
            timeout=self.timeout,
        )
        self._response_json(response, "Quark session refresh")
        self._last_forced_refresh = time.monotonic()

    def _ensure_download_session_fresh(self) -> None:
        age = time.monotonic() - self._last_forced_refresh
        if self._last_forced_refresh == 0.0 or age >= _DOWNLOAD_REFRESH_SECONDS:
            self.refresh_session()

    def list_dir(self, parent_fid: str, page_size: int = 100) -> Iterator[QuarkItem]:
        page = 1
        seen: set[str] = set()
        while True:
            query = dict(QUARK_COMMON_QUERY)
            query.update(
                {
                    "pdir_fid": parent_fid,
                    "_page": str(page),
                    "_size": str(page_size),
                    "_fetch_total": "1",
                    "_sort": "file_type:asc,updated_at:desc",
                }
            )
            response = self.session.get(
                QUARK_BASE + "/file/sort",
                params=query,
                headers=self._cookie_header(),
                timeout=self.timeout,
            )
            payload = self._response_json(response, "Quark list")
            items = (payload.get("data") or {}).get("list") or []
            if not items:
                break
            fresh = 0
            for raw in items:
                fid = str(raw.get("fid") or "")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                fresh += 1
                is_dir = str(raw.get("file_type")) == "0"
                name = str(raw.get("file_name") or raw.get("filename") or "")
                if not name:
                    continue
                yield QuarkItem(
                    fid=fid,
                    name=name,
                    path=f"/{name}",
                    size=int(raw.get("size") or 0),
                    is_dir=is_dir,
                    updated_at=raw.get("updated_at"),
                )
            if len(items) < page_size or fresh == 0:
                break
            page += 1

    def walk(
        self,
        parent_fid: str = "0",
        *,
        base_path: str = "",
        max_depth: int = 8,
        max_nodes: int = 100_000,
    ) -> Iterator[QuarkItem]:
        stack: list[tuple[str, str, int]] = [(parent_fid, base_path, 0)]
        scanned = 0
        while stack:
            pdir_fid, current_path, depth = stack.pop()
            if depth > max_depth:
                continue
            for item in self.list_dir(pdir_fid):
                scanned += 1
                if scanned > max_nodes:
                    raise QuarkError(
                        f"Quark traversal exceeded the safety limit of {max_nodes} nodes"
                    )
                path = _join_path(current_path, item.name)
                current = QuarkItem(
                    fid=item.fid,
                    name=item.name,
                    path=path,
                    size=item.size,
                    is_dir=item.is_dir,
                    updated_at=item.updated_at,
                )
                yield current
                if item.is_dir and depth < max_depth:
                    stack.append((item.fid, path, depth + 1))

    def search(
        self,
        keyword: str,
        *,
        parent_fid: str = "0",
        max_depth: int = 8,
        max_nodes: int = 100_000,
        files_only: bool = True,
    ) -> List[QuarkItem]:
        needle = keyword.casefold()
        hits: list[QuarkItem] = []
        for item in self.walk(
            parent_fid,
            max_depth=max_depth,
            max_nodes=max_nodes,
        ):
            if files_only and item.is_dir:
                continue
            if needle in item.name.casefold():
                hits.append(item)
        return hits

    def resolve_path(self, source_path: str, *, root_fid: str = "0") -> QuarkItem:
        parts = [part for part in source_path.replace("\\", "/").split("/") if part]
        if not parts:
            raise QuarkError("source_path cannot be empty")
        current_fid = root_fid
        current: Optional[QuarkItem] = None
        for index, part in enumerate(parts):
            matches = [item for item in self.list_dir(current_fid) if item.name == part]
            if not matches:
                raise QuarkError(f"Quark path segment not found: {part}")
            if len(matches) > 1:
                raise QuarkError(f"Quark path segment is ambiguous: {part}")
            current = matches[0]
            if index < len(parts) - 1:
                if not current.is_dir:
                    raise QuarkError(f"Quark path segment is not a folder: {part}")
                current_fid = current.fid
        assert current is not None
        return current

    def expand(self, item: QuarkItem, *, max_depth: int, max_nodes: int) -> List[QuarkItem]:
        if not item.is_dir:
            return [item]
        return [
            child
            for child in self.walk(
                item.fid,
                base_path=item.path,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
            if not child.is_dir
        ]

    def get_download_items(self, fids: Iterable[str]) -> List[Dict[str, Any]]:
        requested = [fid for fid in fids if fid]
        if not requested:
            return []
        self._ensure_download_session_fresh()
        response = self.session.post(
            QUARK_DOWNLOAD,
            params={"pr": "ucpro", "fr": "pc"},
            json={"fids": requested},
            headers=self._cookie_header(),
            timeout=max(self.timeout, 60),
        )
        payload = self._response_json(response, "Quark download-link request")
        items = payload.get("data") or []
        if not items:
            raise QuarkError(
                "Quark returned no download URL. Confirm the value is a 32-character Web FID."
            )
        return list(items)

    def probe_range(self, url: str, expected_size: int) -> bool:
        try:
            with self.session.get(
                url,
                headers={**self._cookie_header(), "Range": "bytes=0-0"},
                stream=True,
                timeout=self.timeout,
            ) as response:
                self._merge_cookie_updates(response)
                if response.status_code == 412:
                    raise QuarkError("Quark CDN probe failed with HTTP 412")
                if response.status_code != 206:
                    return False
                content_range = response.headers.get("Content-Range") or ""
                if not content_range.startswith("bytes 0-0/"):
                    return False
                total = content_range.rsplit("/", 1)[1]
                if total.isdigit() and int(total) != expected_size:
                    return False
                next(response.iter_content(1), b"")
                return True
        except QuarkError:
            raise
        except requests.RequestException:
            return False

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        with self.session.get(
            url,
            headers={**self._cookie_header(), "Range": f"bytes={start}-{end}"},
            stream=True,
            timeout=max(self.timeout, 120),
        ) as response:
            self._merge_cookie_updates(response)
            if response.status_code != 206:
                raise QuarkError(
                    f"Quark did not honor Range {start}-{end}; HTTP {response.status_code}"
                )
            data = bytearray()
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    data.extend(chunk)
        expected = end - start + 1
        if len(data) != expected:
            raise QuarkError(
                f"Quark Range length mismatch: expected {expected}, got {len(data)}"
            )
        return bytes(data)

    def open_stream(self, url: str) -> requests.Response:
        response = self.session.get(
            url,
            headers=self._cookie_header(),
            stream=True,
            timeout=max(self.timeout, 120),
        )
        self._merge_cookie_updates(response)
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise QuarkError(f"Quark CDN stream failed with HTTP {status}")
        return response
