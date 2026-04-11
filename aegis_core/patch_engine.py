from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from aegis_core.config import MAX_PATCH_CHARS, MAX_PATCH_TARGET_BYTES, PATCH_STRIP_PREFIX
from aegis_core.guardrails import _assert_allowed_path, _norm_abs

# Robust Unified Diff (Patch) Engine
# ----------------------------
_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

class PatchError(Exception):
    pass

@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: List[str]  # includes prefix ' ', '+', '-'

@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: List[Hunk]

def _strip_diff_path(p: str) -> str:
    p = p.strip()
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p

def _parse_unified_diff(diff_text: str) -> List[FilePatch]:
    if not diff_text or not diff_text.strip():
        raise PatchError("diff is empty")
    if len(diff_text) > MAX_PATCH_CHARS:
        raise PatchError(f"diff too large (>{MAX_PATCH_CHARS} chars)")
    # Normalize line endings for parsing, but preserve '\n' within diff logic
    lines = diff_text.splitlines(True)

    patches: List[FilePatch] = []
    i = 0
    current: Optional[FilePatch] = None

    def require_current():
        if current is None:
            raise PatchError("diff missing file header (--- / +++) before hunks")

    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            old_path = line[4:].strip()
            i += 1
            if i >= len(lines) or not lines[i].startswith("+++ "):
                raise PatchError("diff missing +++ line after ---")
            new_path = lines[i][4:].strip()
            i += 1
            if PATCH_STRIP_PREFIX:
                old_path = _strip_diff_path(old_path)
                new_path = _strip_diff_path(new_path)
            current = FilePatch(old_path=old_path, new_path=new_path, hunks=[])
            patches.append(current)
            continue

        if line.startswith("@@"):
            require_current()
            m = _HUNK_RE.match(line.strip("\r\n"))
            if not m:
                raise PatchError(f"invalid hunk header: {line.strip()}")
            old_start = int(m.group(1))
            old_len = int(m.group(2) or "1")
            new_start = int(m.group(3))
            new_len = int(m.group(4) or "1")
            i += 1
            hunk_lines: List[str] = []
            while i < len(lines):
                l2 = lines[i]
                if l2.startswith("@@") or l2.startswith("--- "):
                    break
                if l2.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
                if not l2:
                    i += 1
                    continue
                prefix = l2[0]
                if prefix not in (" ", "+", "-"):
                    # allow empty line with no prefix? treat as context fail
                    raise PatchError(f"invalid hunk line prefix: {l2[:40]!r}")
                hunk_lines.append(l2)
                i += 1

            current.hunks.append(Hunk(old_start=old_start, old_len=old_len, new_start=new_start, new_len=new_len, lines=hunk_lines))
            continue

        # skip other diff metadata (diff --git, index, etc.)
        i += 1

    if not patches:
        raise PatchError("diff contained no file patches")
    return patches

def _apply_file_patch_to_text(original_text: str, fp: FilePatch) -> str:
    # Work line-based with preserved endings
    orig_lines = original_text.splitlines(True)
    out_lines: List[str] = []
    orig_i = 0  # 0-based index

    for h in fp.hunks:
        # Convert 1-based old_start to 0-based
        target_index = max(0, h.old_start - 1)

        # Emit unchanged lines up to target_index
        if target_index < orig_i:
            raise PatchError("hunk overlaps earlier hunk application (out of order)")
        out_lines.extend(orig_lines[orig_i:target_index])
        orig_i = target_index

        # Apply hunk lines with strict validation
        for hl in h.lines:
            prefix = hl[0]
            content = hl[1:]  # includes newline if present
            if prefix == " ":
                # context must match
                if orig_i >= len(orig_lines):
                    raise PatchError("context beyond end of file")
                if orig_lines[orig_i] != content:
                    raise PatchError(f"context mismatch at line {orig_i+1}: expected {content!r}, got {orig_lines[orig_i]!r}")
                out_lines.append(orig_lines[orig_i])
                orig_i += 1
            elif prefix == "-":
                # removal must match
                if orig_i >= len(orig_lines):
                    raise PatchError("removal beyond end of file")
                if orig_lines[orig_i] != content:
                    raise PatchError(f"remove mismatch at line {orig_i+1}: expected {content!r}, got {orig_lines[orig_i]!r}")
                orig_i += 1
            elif prefix == "+":
                out_lines.append(content)
            else:
                raise PatchError(f"unexpected hunk prefix: {prefix}")

    # Emit remaining lines
    out_lines.extend(orig_lines[orig_i:])
    return "".join(out_lines)

def _apply_unified_diff_to_path(target_path: str, diff_text: str) -> Dict[str, Any]:
    ap = _assert_allowed_path(target_path)

    if not os.path.exists(ap):
        raise PatchError("target file does not exist")
    if os.path.isdir(ap):
        raise PatchError("target path is a directory")

    size = os.path.getsize(ap)
    if size > MAX_PATCH_TARGET_BYTES:
        raise PatchError(f"target file too large ({size} bytes). Max is {MAX_PATCH_TARGET_BYTES}")

    with open(ap, "rb") as f:
        raw = f.read()
    # detect encoding simply; default utf-8 with replacement
    try:
        original_text = raw.decode("utf-8")
        encoding = "utf-8"
    except Exception:
        original_text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8(replace)"

    patches = _parse_unified_diff(diff_text)

    # Choose which file patch applies to target_path:
    # We accept if old_path or new_path basename matches, or normalized absolute matches after stripping prefixes.
    target_norm = _norm_abs(ap).lower()

    chosen: Optional[FilePatch] = None
    for fp in patches:
        cand_old = fp.old_path.strip()
        cand_new = fp.new_path.strip()
        # Strip a/ b/ prefix if present
        cand_old2 = _strip_diff_path(cand_old) if (cand_old.startswith("a/") or cand_old.startswith("b/")) else cand_old
        cand_new2 = _strip_diff_path(cand_new) if (cand_new.startswith("a/") or cand_new.startswith("b/")) else cand_new

        # If diff uses absolute windows path, compare directly
        try:
            if _norm_abs(cand_old2).lower() == target_norm or _norm_abs(cand_new2).lower() == target_norm:
                chosen = fp
                break
        except Exception:
            pass

        # Otherwise compare basename
        if os.path.basename(cand_old2).lower() == os.path.basename(ap).lower() or os.path.basename(cand_new2).lower() == os.path.basename(ap).lower():
            chosen = fp
            break

    if chosen is None:
        if len(patches) == 1:
            chosen = patches[0]
        else:
            raise PatchError("diff does not clearly target the specified file (and diff contains multiple file patches)")

    new_text = _apply_file_patch_to_text(original_text, chosen)

    # Write safely
    tmp_path = ap + ".patchtmp"
    with open(tmp_path, "w", encoding="utf-8", errors="replace", newline="") as f:
        f.write(new_text)
    os.replace(tmp_path, ap)

    return {
        "ok": True,
        "path": ap,
        "encoding_read": encoding,
        "bytes_before": size,
        "bytes_after": len(new_text.encode("utf-8", errors="replace")),
        "file_old": chosen.old_path,
        "file_new": chosen.new_path,
        "hunks": len(chosen.hunks),
    }
