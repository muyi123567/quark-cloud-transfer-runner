from __future__ import annotations

import time
from collections.abc import Iterator
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional

import requests

from .errors import QuarkCdnError, QuarkError
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

# The Quark download CDN is a mainland node pool that the GitHub runner reaches
# over a cross-border path. A long connect timeout only wastes the run window,
# so connect failures are detected quickly and turned into a fresh-URL retry.
CDN_CONNECT_TIMEOUT_SECONDS = 20.0
CDN_READ_TIMEOUT_SECONDS = 120.0

# Signed-URL responses that mean "this URL is no longer usable": request a new
# one instead of hammering the same host.
_SIGNED_URL_REJECTED_STATUSES = (401, 403, 412)


def _join_path(base: str, name: str) -> str:
    if not base:
        return f"/{name}"
    return f"{base.rstrip('/')}/{name}"


def _extract_content_hash(raw: Dict[str, Any]) -> Optional[str]:
    for key in ("md5", "md5_hash", "file_md5", "sha1", "sha1_hash", "hash"):
        value = raw.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


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
        cdn_connect_timeout: float = CDN_CONNECT_TIMEOUT_SECONDS,
        cdn_read_timeout: float = CDN_READ_TIMEOUT_SECONDS,
        proxy: Optional[str] = None,
    ) -> None:
        if cdn_connect_timeout <= 0 or cdn_read_timeout <= 0:
            raise QuarkError("CDN timeouts must be positive")
        self.timeout = timeout
        self.cdn_connect_timeout = cdn_connect_timeout
        self.cdn_read_timeout = cdn_read_timeout
        self.proxy = (proxy or "").strip() or None
        self.session = session or requests.Session()
        # trust_env stays False so ambient runner proxies are never picked up
        # implicitly, but an explicit proxy is honored: the Quark CDN leg is a
        # mainland link and is the part worth routing through a better egress.
        self.session.trust_env = False
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})
        self._state_lock = RLock()
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

    def _cdn_timeout(self) -> tuple[float, float]:
        """(connect, read) timeout pair for Quark CDN requests."""

        return (self.cdn_connect_timeout, self.cdn_read_timeout)

    def _cookie_header(self, *, omit: set[str] | None = None) -> dict[str, str]:
        return {"Cookie": _format_cookie(self._cookies, omit=omit)}

    def _merge_cookie_updates(self, response: requests.Response) -> None:
        response_cookies = getattr(response, "cookies", None)
        if response_cookies is None:
            return
        # Rotating cookies are shared state: several worker threads may stream
        # from the CDN at the same time.
        with self._state_lock:
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
        # One rotation at a time: concurrent rotations would fight over __puus.
        with self._state_lock:
            response = self.session.get(
                QUARK_CONFIG,
                params=QUARK_COMMON_QUERY,
                headers=self._cookie_header(omit={"__puus"}),
                timeout=self.timeout,
            )
            self._response_json(response, "Quark session refresh")
            self._last_forced_refresh = time.monotonic()

    def _ensure_download_session_fresh(self) -> None:
        with self._state_lock:
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
                    content_hash=_extract_content_hash(raw),
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
                    content_hash=item.content_hash,
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
        """Search the whole Quark drive through the native search endpoint.

        Quark's web UI exposes virtual groupings that are not always reachable
        by walking pdir_fid=0. The native /file/search endpoint sees those files
        and is also much faster than recursively listing the whole drive.
        parent_fid/max_depth are retained for call compatibility; global search
        is intentionally drive-wide.
        """
        del parent_fid, max_depth
        needle = keyword.casefold()
        hits: list[QuarkItem] = []
        seen: set[str] = set()
        page = 1
        page_size = 100
        scanned = 0

        while True:
            query = dict(QUARK_COMMON_QUERY)
            query.update(
                {
                    "q": keyword,
                    "_page": str(page),
                    "_size": str(page_size),
                    "_fetch_total": "1",
                    "_sort": "file_type:desc,updated_at:desc",
                    "_is_hl": "1",
                }
            )
            response = self.session.get(
                QUARK_BASE + "/file/search",
                params=query,
                headers=self._cookie_header(),
                timeout=self.timeout,
            )
            payload = self._response_json(response, "Quark search")
            items = (payload.get("data") or {}).get("list") or []
            if not items:
                break

            fresh = 0
            for raw in items:
                scanned += 1
                if scanned > max_nodes:
                    raise QuarkError(
                        f"Quark search exceeded the safety limit of {max_nodes} nodes"
                    )
                fid = str(raw.get("fid") or "")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                fresh += 1
                is_dir = str(raw.get("file_type")) == "0"
                if files_only and is_dir:
                    continue
                name = str(raw.get("file_name") or raw.get("filename") or "")
                if not name or needle not in name.casefold():
                    continue
                path = str(raw.get("file_path") or raw.get("path") or f"/{name}")
                if not path.startswith("/"):
                    path = "/" + path
                hits.append(
                    QuarkItem(
                        fid=fid,
                        name=name,
                        path=path,
                        size=int(raw.get("size") or 0),
                        is_dir=is_dir,
                        updated_at=raw.get("updated_at"),
                    )
                )

            if len(items) < page_size or fresh == 0:
                break
            page += 1

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
        """Report whether this signed URL honors bounded Range requests.

        ``False`` means the URL is reachable but not range-capable, so the
        caller must use one continuous response. ``QuarkCdnError`` means the CDN
        could not be reached or the signature expired, so the caller should
        request a fresh download URL for the same FID and retry.
        """
        try:
            with self.session.get(
                url,
                headers={**self._cookie_header(), "Range": "bytes=0-0"},
                stream=True,
                timeout=self._cdn_timeout(),
            ) as response:
                self._merge_cookie_updates(response)
                if response.status_code in _SIGNED_URL_REJECTED_STATUSES:
                    raise QuarkCdnError(
                        "Quark CDN rejected the signed download URL with "
                        f"HTTP {response.status_code}"
                    )
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
        except QuarkCdnError:
            raise
        except requests.RequestException as exc:
            raise QuarkCdnError(f"Quark CDN range probe failed: {exc}") from exc

    def fetch_range(self, url: str, start: int, end: int) -> bytes:
        try:
            with self.session.get(
                url,
                headers={**self._cookie_header(), "Range": f"bytes={start}-{end}"},
                stream=True,
                timeout=self._cdn_timeout(),
            ) as response:
                self._merge_cookie_updates(response)
                if response.status_code in _SIGNED_URL_REJECTED_STATUSES:
                    raise QuarkCdnError(
                        "Quark CDN rejected the signed download URL with "
                        f"HTTP {response.status_code}"
                    )
                if response.status_code != 206:
                    raise QuarkCdnError(
                        f"Quark did not honor Range {start}-{end}; "
                        f"HTTP {response.status_code}"
                    )
                data = bytearray()
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        data.extend(chunk)
        except QuarkCdnError:
            raise
        except requests.RequestException as exc:
            raise QuarkCdnError(
                f"Quark Range {start}-{end} fetch failed: {exc}"
            ) from exc
        expected = end - start + 1
        if len(data) != expected:
            raise QuarkCdnError(
                f"Quark Range length mismatch: expected {expected}, got {len(data)}"
            )
        return bytes(data)

    def open_stream(self, url: str) -> requests.Response:
        try:
            response = self.session.get(
                url,
                headers=self._cookie_header(),
                stream=True,
                timeout=self._cdn_timeout(),
            )
        except requests.RequestException as exc:
            raise QuarkCdnError(
                f"Quark CDN stream connection failed: {exc}"
            ) from exc
        self._merge_cookie_updates(response)
        if response.status_code in _SIGNED_URL_REJECTED_STATUSES:
            status = response.status_code
            response.close()
            raise QuarkCdnError(
                f"Quark CDN rejected the signed download URL with HTTP {status}"
            )
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise QuarkError(f"Quark CDN stream failed with HTTP {status}")
        return response
