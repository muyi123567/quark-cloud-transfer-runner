from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:access|refresh|id)_token[\"']?\s*[:=]\s*[\"']?)[^\"',;\s}]+"
    ),
    re.compile(
        r"(?i)((?:client_secret|authorization|cookie)[\"']?\s*[:=]\s*[\"']?)[^\"',;\s}]+"
    ),
    re.compile(r"(?i)(__puus=)[^;\s]+"),
    re.compile(r"(?i)(__pus=)[^;\s]+"),
    re.compile(
        r"(?i)(https://[^/\s]+\.drive\.quark\.cn/[^?\s\"']+)\?[^\s\"']+"
    ),
    re.compile(r"(?i)([?&](?:auth_key|token|ork)=)[^&\s\"']+"),
    # The bare signed-download path that requests embeds in exception text,
    # e.g. "url: /8JImeSAf/5439150331/530557ed.../...?abt=8_0_&auth_key=...".
    re.compile(r"(?i)(url:\s*)(/[^\s?]*\?[^\s\"']*)"),
    # Credentials embedded in a proxy URL, e.g. "http://user:pass@host:3128".
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
]


def redact_text(value: object) -> str:
    """Remove common credential shapes before text reaches logs."""

    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text
