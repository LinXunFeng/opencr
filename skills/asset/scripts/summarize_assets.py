#!/usr/bin/env python3
"""Summarize changed image assets from OpenCR skill payload JSON."""

import json
import pathlib
import sys


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif", ".heic"}
LIMIT_KB = 300


def main() -> int:
    payload = json.load(sys.stdin)
    changes = payload.get("changes") or []
    rows = []
    for change in changes:
        path = change.get("new_path") or change.get("old_path") or "unknown"
        suffix = pathlib.Path(path).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            continue
        size_kb = change.get("file_size_kb")
        if size_kb is None and change.get("file_size_bytes") is not None:
            size_kb = round(float(change["file_size_bytes"]) / 1024, 1)
        status = "over-limit" if size_kb is not None and size_kb > LIMIT_KB else "ok-or-unknown"
        rows.append(f"- {path}: size_kb={size_kb}, status={status}")

    if rows:
        print("Changed image asset summary:")
        print("\n".join(rows))
    else:
        print("No changed image assets detected in payload.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
