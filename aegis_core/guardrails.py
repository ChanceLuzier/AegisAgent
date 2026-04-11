from __future__ import annotations

import os

from aegis_core.config import ALLOWED_ROOTS

# ----------------------------
# Filesystem tool safety
# ----------------------------

def _norm_abs(p: str) -> str:
    p = os.path.expandvars(p)
    p = os.path.expanduser(p)
    return os.path.abspath(os.path.normpath(p))


def _is_under(child: str, parent: str) -> bool:
    child = _norm_abs(child)
    parent = _norm_abs(parent)
    try:
        common = os.path.commonpath([child, parent])
    except Exception:
        return False
    return common.lower() == parent.lower()


def _assert_allowed_path(p: str) -> str:
    if not p or not str(p).strip():
        raise ValueError("path required")
    # sanitize trailing punctuation (e.g. C:\AI\director?)
    p2 = str(p).strip().strip('"').strip("'").rstrip(".,!?")
    ap = _norm_abs(p2)
    for root in ALLOWED_ROOTS:
        if _is_under(ap, root):
            return ap
    raise PermissionError(f"Path not allowed. Allowed roots: {ALLOWED_ROOTS}")


def _is_allowed_path(p: str) -> bool:
    """Boolean variant of _assert_allowed_path for read-only operations (no exceptions)."""
    if not p or not str(p).strip():
        return False
    try:
        p2 = str(p).strip().strip('"').strip("'").rstrip(".,!?")
        ap = _norm_abs(p2)
    except Exception:
        return False
    for root in ALLOWED_ROOTS:
        if _is_under(ap, root):
            return True
    return False
