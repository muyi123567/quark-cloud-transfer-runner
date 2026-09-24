#!/usr/bin/env python3
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MODULES = [
    "quark_cloud_transfer",
    "quark_cloud_transfer.config",
    "quark_cloud_transfer.gdrive",
    "quark_cloud_transfer.quark",
    "quark_cloud_transfer.redaction",
    "quark_cloud_transfer.transfer",
    "quark_cloud_transfer.cli",
]


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python>=3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
    checks.append(("requests", importlib.util.find_spec("requests") is not None, ""))
    for module in MODULES:
        try:
            importlib.import_module(module)
            checks.append((module, True, ""))
        except Exception as exc:
            checks.append((module, False, str(exc)))

    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        suffix = f": {detail}" if detail else ""
        print(("OK" if passed else "FAIL") + f"  {name}{suffix}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
