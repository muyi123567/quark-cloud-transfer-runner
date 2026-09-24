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
]


def redact_text(value: object) -> str:
    """Remove common credential shapes before text reaches logs."""

    text = str(value)
    for pattern in _PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text
