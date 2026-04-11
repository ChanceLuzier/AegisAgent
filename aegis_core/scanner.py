from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from aegis_core.config import DIRECTOR_DIR, STATIC_DIR
from aegis_core.guardrails import _is_allowed_path

# ----------------------------
# Version scan (grounded)
# ----------------------------
_LAST_VERSION_SCAN: Dict[str, Any] = {"items": []}

def _tool_scan_project_versions(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Grounded scan for semver-like app versions in real project files.
    - Defaults to matching v-prefixed semver: v1.2.3 (avoids IP fragments like 127.0.0.1)
    - Scans only within DIRECTOR_DIR and STATIC_DIR
    """
    pattern = str(args.get("pattern") or r"\bv\d+\.\d+\.\d+\b")
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"ok": False, "error": f"Invalid regex pattern: {e}"}

    # File extension allowlist
    exts = tuple(args.get("exts") or [".py", ".html", ".js", ".css", ".md", ".txt", ".svg"])
    max_files = int(args.get("max_files") or 500)
    max_matches_per_file = int(args.get("max_matches_per_file") or 50)

    roots = [DIRECTOR_DIR, STATIC_DIR]
    seen_files = set()
    items = []

    def _should_skip(rel_path: str) -> bool:
        rel = rel_path.replace("\\", "/")
        return rel.startswith("sessions/") or rel.startswith("tmp/") or rel.startswith("__pycache__/")

    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if len(seen_files) >= max_files:
                    break
                if not fn.lower().endswith(exts):
                    continue
                full_path = os.path.join(dirpath, fn)
                # ensure inside allowed roots
                if not _is_allowed_path(full_path):
                    continue
                rel = os.path.relpath(full_path, DIRECTOR_DIR)
                if _should_skip(rel):
                    continue
                if full_path in seen_files:
                    continue
                seen_files.add(full_path)

                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue

                matches = rx.findall(text) or []
                if not matches:
                    continue

                # Deduplicate & cap
                uniq = []
                for m in matches:
                    if m not in uniq:
                        uniq.append(m)
                    if len(uniq) >= max_matches_per_file:
                        break

                items.append({"path": rel.replace("\\", "/"), "matches": uniq})

    items.sort(key=lambda x: x["path"])
    global _LAST_VERSION_SCAN
    _LAST_VERSION_SCAN = {"items": items}

    return {"ok": True, "items": items, "count_files": len(items)}

def _tool_get_last_version_scan(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, **_LAST_VERSION_SCAN}
