"""
Aegis AI — v1.1.7 (Modular Core)
Semi-Autonomous Local Assistant Core (Windows, GPU, Local-first)

Run:
  .venv\\Scripts\\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000

UI:
  http://127.0.0.1:8000/ui

This build adds a robust patch workflow:
- propose_patch (SAFE): validate unified diff and create approval request for apply_patch
- apply_patch (RISKY): robustly applies unified diff hunks with context validation

Also includes:
- Path sanitizer for punctuation (C:\\AI\\director? -> C:\\AI\\director)
- Burn session removes backend + UI + pending approvals
- Direct-return for list_dir/read_file to avoid “Done.”
"""

from __future__ import annotations

import os
import re
import json
import time
import uuid
import glob
import wave
import traceback
import threading
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, Tuple

import numpy as np
import requests
from fastapi import FastAPI, Body, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from aegis_core.config import (
    APP_NAME,
    APP_VERSION,
    APP_TITLE,
    DIRECTOR_DIR,
    TMP_DIR,
    SESSIONS_DIR,
    STATIC_DIR,
    OLLAMA_CHAT_URL,
    OLLAMA_MODEL,
    VOICE_SR,
    KOKORO_VOICE,
    KOKORO_GPU,
    KOKORO_PREWARM,
    UI_MAX_BUBBLES,
    TMP_MAX_FILES,
    TMP_MAX_AGE_SEC,
    AUTO_VOICE_DEFAULT_ON,
    AEGIS_MODE,
    AGENT_MAX_ITERS,
    ALLOWED_ROOTS,
    MAX_READ_BYTES,
    MAX_WRITE_BYTES,
    PROCESS_TIMEOUT_SEC,
    MAX_PROCESS_OUTPUT_CHARS,
    MAX_PATCH_CHARS,
    MAX_PATCH_TARGET_BYTES,
    PATCH_STRIP_PREFIX,
    DIRECT_RETURN_TOOLS,
)

from aegis_core.guardrails import (
    _norm_abs,
    _is_under,
    _assert_allowed_path,
    _is_allowed_path,
)

from aegis_core.scanner import (
    _tool_scan_project_versions,
    _tool_get_last_version_scan,
)
from aegis_core.patch_engine import (
    PatchError,
    _strip_diff_path,
    _parse_unified_diff,
    _apply_unified_diff_to_path,
)

# ----------------------------
# Globals
# ----------------------------
app = FastAPI(title=APP_TITLE, version=APP_VERSION)

_last_error: str = ""

_tts = None
_tts_ready = False

_PREFS_FILE = os.path.join(DIRECTOR_DIR, "data", "user_prefs.json")

def _load_prefs() -> Dict[str, Any]:
    try:
        with open(_PREFS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_prefs(prefs: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_PREFS_FILE), exist_ok=True)
        with open(_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass

_active_voice: str = _load_prefs().get("voice", KOKORO_VOICE)

_stt_model = None
_stt_ready = False

_app_ready = False
_app_ready_detail = "Starting..."
_warm_thread_started = False

# approval_id -> dict(session_id, tool, args, reason, agent_msgs/heuristic flags, diff preview, etc.)
PENDING_APPROVALS: Dict[str, Dict[str, Any]] = {}

# ----------------------------
# Helpers
# ----------------------------
def _set_last_error(exc: BaseException) -> None:
    global _last_error
    _last_error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

def _ensure_dirs() -> None:
    os.makedirs(TMP_DIR, exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)

def _now_ts() -> float:
    return time.time()

def _safe_filename(prefix: str, ext: str = "wav") -> str:
    return f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:10]}.{ext}"

def _cleanup_tmp() -> None:
    try:
        now = time.time()
        files: List[Tuple[str, float]] = []
        for p in glob.glob(os.path.join(TMP_DIR, "*.wav")):
            try:
                st = os.stat(p)
                files.append((p, st.st_mtime))
            except OSError:
                continue

        for p, mtime in files:
            if (now - mtime) > TMP_MAX_AGE_SEC:
                try:
                    os.remove(p)
                except OSError:
                    pass

        files = []
        for p in glob.glob(os.path.join(TMP_DIR, "*.wav")):
            try:
                st = os.stat(p)
                files.append((p, st.st_mtime))
            except OSError:
                continue

        files.sort(key=lambda x: x[1], reverse=True)
        for p, _ in files[TMP_MAX_FILES:]:
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass

def _normalize_session_id(session_id: str) -> str:
    sid = re.sub(r"[^a-zA-Z0-9_\-]", "_", session_id)[:80]
    return sid or "default"

def _session_path(session_id: str) -> str:
    sid = _normalize_session_id(session_id)
    return os.path.join(SESSIONS_DIR, f"{sid}.json")

def _default_session_name(session_id: str) -> str:
    return f"Session {session_id[-6:]}" if len(session_id) >= 6 else f"Session {session_id}"

def _new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:12]

def _sanitize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = re.sub(r"\s{2,}", " ", name)
    return name[:60]

def _load_session(session_id: str) -> Dict[str, Any]:
    sid = _normalize_session_id(session_id)
    path = _session_path(sid)
    if not os.path.exists(path):
        now = _now_ts()
        return {"id": sid, "name": _default_session_name(sid), "created_ts": now, "updated_ts": now, "messages": []}
    with open(path, "r", encoding="utf-8") as f:
        s = json.load(f)
    s.setdefault("id", sid)
    s.setdefault("name", _default_session_name(sid))
    s.setdefault("created_ts", _now_ts())
    s.setdefault("updated_ts", _now_ts())
    s.setdefault("messages", [])
    return s

def _save_session(session: Dict[str, Any]) -> None:
    sid = _normalize_session_id(str(session.get("id") or "default"))
    session["id"] = sid
    session.setdefault("name", _default_session_name(sid))
    session.setdefault("created_ts", _now_ts())
    session["updated_ts"] = _now_ts()
    session.setdefault("messages", [])
    path = _session_path(sid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _list_sessions() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                s = json.load(f)
            sid = _normalize_session_id(str(s.get("id") or os.path.splitext(os.path.basename(path))[0]))
            name = str(s.get("name") or _default_session_name(sid))
            created_ts = float(s.get("created_ts") or 0.0)
            updated_ts = float(s.get("updated_ts") or created_ts or 0.0)
            msg_count = int(len(s.get("messages") or []))
            out.append({"id": sid, "name": name, "created_ts": created_ts, "updated_ts": updated_ts, "message_count": msg_count})
        except Exception:
            continue
    out.sort(key=lambda x: x.get("updated_ts", 0.0), reverse=True)
    return out

def _delete_session_file(session_id: str) -> bool:
    sid = _normalize_session_id(session_id)
    path = _session_path(sid)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True

def _purge_pending_for_session(session_id: str) -> int:
    sid = _normalize_session_id(session_id)
    to_delete = []
    for appr_id, item in PENDING_APPROVALS.items():
        if _normalize_session_id(str(item.get("session_id") or "")) == sid:
            to_delete.append(appr_id)
    for appr_id in to_delete:
        PENDING_APPROVALS.pop(appr_id, None)
    return len(to_delete)

def _burn_session_everything(session_id: str) -> Dict[str, Any]:
    sid = _normalize_session_id(session_id)
    deleted = _delete_session_file(sid)
    purged = _purge_pending_for_session(sid)
    return {"ok": True, "burned": bool(deleted), "deleted": bool(deleted), "session_id": sid, "purged_pending": purged}

def _ollama_chat(messages: List[Dict[str, str]]) -> str:
    payload = {"model": OLLAMA_MODEL, "messages": messages, "stream": False}
    r = requests.post(OLLAMA_CHAT_URL, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()
    return (data.get("message") or {}).get("content", "").strip()

def _ollama_chat_retry(messages: List[Dict[str, str]]) -> str:
    out = _ollama_chat(messages)
    if out.strip():
        return out.strip()
    time.sleep(0.15)
    return (_ollama_chat(messages) or "").strip()

def _write_wav(path: str, audio: Any, sr: int) -> None:
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm16 = (arr * 32767.0).astype(np.int16).tobytes()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16)

def _init_tts() -> None:
    global _tts, _tts_ready
    if _tts_ready:
        return
    from kokoro import KPipeline
    device = "cuda" if KOKORO_GPU else "cpu"
    _tts = KPipeline(lang_code="a", device=device)
    _tts_ready = True
    if KOKORO_PREWARM:
        try:
            for _ in _tts("Warmup.", voice=_active_voice, speed=1.0):
                pass
        except Exception:
            pass

def _synthesize_full(text: str) -> str:
    if not text.strip():
        raise ValueError("No text to synthesize")
    _cleanup_tmp()
    _init_tts()
    filename = _safe_filename("voice", "wav")
    out_path = os.path.join(TMP_DIR, filename)
    chunks = [audio for _, _, audio in _tts(text, voice=_active_voice, speed=1.0)]
    audio = np.concatenate(chunks)
    _write_wav(out_path, audio, VOICE_SR)
    return f"/tmp_audio/{filename}"
def _truncate(s: str, n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    if len(s) <= n:
        return s
    return s[:n] + f"\n…(truncated {len(s)-n} chars)"

# ----------------------------

# ----------------------------
# Tool Registry
# ----------------------------
from aegis_core.tools import ToolDef, ToolLevel
from aegis_core.tools_registry import build_system_tool_instructions, build_tools_registry, get_mcp_tools

def _tool_level(name: str) -> ToolLevel:
    from aegis_core.tools import tool_level
    return tool_level(TOOLS, name)

def _requires_approval(tool_name: str) -> bool:
    from aegis_core.tools import requires_approval
    return requires_approval(AEGIS_MODE, TOOLS, tool_name)

# ---------- SAFE tools ----------
def _tool_summarize_session(args: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    s = _load_session(session_id)
    msgs = s.get("messages", []) or []
    last = msgs[-12:] if len(msgs) > 12 else msgs
    lines: List[str] = []
    for m in last:
        role = m.get("role", "?")
        content = (m.get("content", "") or "").strip()
        if not content:
            continue
        content = re.sub(r"\s+", " ", content)
        if len(content) > 180:
            content = content[:180] + "…"
        lines.append(f"- {role}: {content}")
    return {"ok": True, "session_id": _normalize_session_id(session_id), "name": s.get("name", ""), "summary": "\n".join(lines) if lines else "(empty)"}

def _tool_search_sessions(args: Dict[str, Any]) -> Dict[str, Any]:
    q = str(args.get("query") or "").strip().lower()
    sessions = _list_sessions()
    if not q:
        return {"ok": True, "results": sessions[:50]}
    out = []
    for item in sessions:
        if q in (item.get("name") or "").lower() or q in (item.get("id") or "").lower():
            out.append(item)
            continue
        try:
            s = _load_session(item["id"])
            for m in s.get("messages", []) or []:
                if q in (m.get("content") or "").lower():
                    out.append(item)
                    break
        except Exception:
            continue
    return {"ok": True, "query": q, "results": out[:50]}

def _tool_rename_session(args: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(args.get("session_id") or "").strip()
    new_name = _sanitize_name(str(args.get("name") or args.get("new_name") or args.get("new_title") or ""))
    if not session_id or not new_name:
        return {"ok": False, "error": "session_id and name required"}
    s = _load_session(session_id)
    s["name"] = new_name
    _save_session(s)
    return {"ok": True, "id": s["id"], "name": s["name"]}

def _tool_format_text(args: Dict[str, Any]) -> Dict[str, Any]:
    text = str(args.get("text") or "")
    style = str(args.get("style") or "clean").lower().strip()
    if style == "clean":
        out = re.sub(r"[ \t]+", " ", text)
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
    elif style == "bullets":
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        out = "\n".join([f"- {ln}" for ln in lines]).strip()
    else:
        out = text.strip()
    return {"ok": True, "style": style, "text": out}



def _tool_propose_patch(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    SAFE tool: validates a unified diff and returns a "proposed_approval" structure,
    which the agent loop converts into an approval bubble for apply_patch (risky).
    """
    path = str(args.get("path") or "").strip()
    diff = args.get("diff")
    if not path:
        return {"ok": False, "error": "path required"}
    if diff is None:
        return {"ok": False, "error": "diff required"}
    diff = str(diff)
    if len(diff) > MAX_PATCH_CHARS:
        return {"ok": False, "error": f"diff too large (>{MAX_PATCH_CHARS} chars)"}

    # validate target file path allowed
    ap = _assert_allowed_path(path)

    # parse/validate diff format
    patches = _parse_unified_diff(diff)

    # extra sanity: ensure diff targets the file (or single file diff)
    if len(patches) > 1:
        # must match specified file; we'll let apply decide too, but propose should be stricter
        target_base = os.path.basename(ap).lower()
        ok = False
        for fp in patches:
            if os.path.basename(_strip_diff_path(fp.old_path)).lower() == target_base or os.path.basename(_strip_diff_path(fp.new_path)).lower() == target_base:
                ok = True
                break
        if not ok:
            return {"ok": False, "error": "diff contains multiple file patches and none match the specified path"}

    # don't apply here; just request approval for apply_patch
    return {
        "ok": True,
        "message": "Patch validated. Approval required to apply.",
        "proposed_approval": {
            "tool": "apply_patch",
            "args": {"path": ap, "diff": diff},
            "diff_preview": diff,
        },
    }

# ---------- RISKY tools ----------
def _tool_burn_session(args: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(args.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    return _burn_session_everything(session_id)

def _tool_list_dir(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path") or args.get("dir") or "").strip()
    ap = _assert_allowed_path(path)
    if not os.path.exists(ap):
        return {"ok": False, "error": "Path does not exist", "path": ap}
    if not os.path.isdir(ap):
        return {"ok": False, "error": "Path is not a directory", "path": ap}

    include_hidden = bool(args.get("include_hidden", False))
    max_items = int(args.get("max_items") or 200)
    max_items = max(1, min(max_items, 2000))

    items = []
    for name in os.listdir(ap):
        if not include_hidden and name.startswith("."):
            continue
        full = os.path.join(ap, name)
        try:
            st = os.stat(full)
            items.append({
                "name": name,
                "path": full,
                "is_dir": os.path.isdir(full),
                "size": int(getattr(st, "st_size", 0)),
                "mtime": float(getattr(st, "st_mtime", 0.0)),
            })
        except Exception:
            items.append({"name": name, "path": full, "is_dir": os.path.isdir(full)})
        if len(items) >= max_items:
            break

    items.sort(key=lambda x: (not x.get("is_dir", False), (x.get("name") or "").lower()))
    return {"ok": True, "path": ap, "count": len(items), "items": items}

def _tool_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path") or "").strip()
    ap = _assert_allowed_path(path)
    if not os.path.exists(ap):
        return {"ok": False, "error": "File does not exist", "path": ap}
    if os.path.isdir(ap):
        return {"ok": False, "error": "Path is a directory", "path": ap}
    enc = str(args.get("encoding") or "utf-8")
    size = os.path.getsize(ap)
    if size > MAX_READ_BYTES:
        return {"ok": False, "error": f"File too large ({size} bytes). Max is {MAX_READ_BYTES}.", "path": ap, "size": size}
    with open(ap, "r", encoding=enc, errors="replace") as f:
        data = f.read()
    return {"ok": True, "path": ap, "size": size, "encoding": enc, "content": data}

def _tool_write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path") or "").strip()
    ap = _assert_allowed_path(path)
    content = args.get("content")
    if content is None:
        return {"ok": False, "error": "content required", "path": ap}
    content = str(content)
    if len(content.encode("utf-8", errors="replace")) > MAX_WRITE_BYTES:
        return {"ok": False, "error": f"Content too large. Max is {MAX_WRITE_BYTES} bytes.", "path": ap}
    enc = str(args.get("encoding") or "utf-8")
    mkdirs = bool(args.get("mkdirs", True))
    parent = os.path.dirname(ap)
    if mkdirs and parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    with open(ap, "w", encoding=enc, errors="replace") as f:
        f.write(content)
    return {"ok": True, "path": ap, "bytes_written": len(content.encode('utf-8', errors='replace'))}

def _tool_run_external_process(args: Dict[str, Any]) -> Dict[str, Any]:
    cmd = args.get("cmd") or args.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        return {"ok": False, "error": "cmd must be a non-empty array of strings"}

    cwd = args.get("cwd")
    if cwd:
        try:
            cwd = _assert_allowed_path(str(cwd))
        except Exception as e:
            return {"ok": False, "error": f"cwd not allowed: {e}"}
    else:
        cwd = DIRECTOR_DIR

    timeout = int(args.get("timeout_sec") or PROCESS_TIMEOUT_SEC)
    timeout = max(1, min(timeout, 300))

    env_overrides = args.get("env")
    env = None
    if isinstance(env_overrides, dict):
        env = os.environ.copy()
        for k, v in env_overrides.items():
            if isinstance(k, str):
                env[k] = str(v)

    try:
        cp = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, shell=False)
        stdout = _truncate(cp.stdout, MAX_PROCESS_OUTPUT_CHARS)
        stderr = _truncate(cp.stderr, MAX_PROCESS_OUTPUT_CHARS)
        return {"ok": True, "cmd": cmd, "cwd": cwd, "returncode": cp.returncode, "stdout": stdout, "stderr": stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Process timed out after {timeout}s", "cmd": cmd, "cwd": cwd}
    except Exception as e:
        return {"ok": False, "error": str(e), "cmd": cmd, "cwd": cwd}

def _tool_apply_patch(args: Dict[str, Any]) -> Dict[str, Any]:
    path = str(args.get("path") or "").strip()
    diff = args.get("diff")
    if not path:
        return {"ok": False, "error": "path required"}
    if diff is None:
        return {"ok": False, "error": "diff required"}
    diff = str(diff)
    try:
        result = _apply_unified_diff_to_path(path, diff)
        return result
    except PatchError as pe:
        return {"ok": False, "error": str(pe), "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": path}

def _tool_stub(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": False, "error": "Tool not implemented in v1.x", "args": args}

TOOLS: Dict[str, "ToolDef"] = build_tools_registry(
    tool_summarize_session=_tool_summarize_session,
    tool_search_sessions=_tool_search_sessions,
    tool_rename_session=_tool_rename_session,
    tool_format_text=_tool_format_text,
    tool_scan_project_versions=_tool_scan_project_versions,
    tool_get_last_version_scan=_tool_get_last_version_scan,
    tool_propose_patch=_tool_propose_patch,
    tool_burn_session=_tool_burn_session,
    tool_list_dir=_tool_list_dir,
    tool_read_file=_tool_read_file,
    tool_write_file=_tool_write_file,
    tool_run_external_process=_tool_run_external_process,
    tool_apply_patch=_tool_apply_patch,
    tool_stub=_tool_stub,
)

def _execute_tool(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    from aegis_core.tools import execute_tool
    return execute_tool(TOOLS, tool_name, args)

# ----------------------------
# Tool parsing + prompting
# ----------------------------
SYSTEM_TOOL_INSTRUCTIONS = build_system_tool_instructions(APP_NAME, ALLOWED_ROOTS, TOOLS)

FORCE_FINAL_INSTRUCTION = (
    "STOP USING TOOLS NOW.\n"
    "Respond normally with a helpful final answer to the user's last message.\n"
    "Do not output JSON.\n"
)

def _build_agent_messages(session_messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_TOOL_INSTRUCTIONS}]
    for m in session_messages:
        role = m.get("role")
        if role in ("user", "assistant"):
            msgs.append({"role": role, "content": str(m.get("content") or "")})
    return msgs

def _extract_first_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None

def _try_parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "tool" in obj and "args" in obj:
            return obj
    except Exception:
        pass
    blob = _extract_first_json_object(cleaned)
    if not blob:
        return None
    try:
        obj = json.loads(blob)
        if isinstance(obj, dict) and "tool" in obj and "args" in obj:
            return obj
    except Exception:
        return None
    return None

def _force_final_answer(agent_msgs: List[Dict[str, str]]) -> str:
    forced = list(agent_msgs)
    forced.append({"role": "system", "content": FORCE_FINAL_INSTRUCTION})
    return _ollama_chat_retry(forced)

# ----------------------------
# Heuristic tool suggestion (semi)
# ----------------------------
_WIN_PATH_RE = re.compile(r"([a-zA-Z]:\\[^\n\r\t]+)")
def _heuristic_tool_suggestion(user_text: str) -> Optional[Dict[str, Any]]:
    if not user_text:
        return None
    t = user_text.strip().lower()
    m = _WIN_PATH_RE.search(user_text)
    if not m:
        return None
    path = m.group(1).strip().strip('"').strip("'")
    path = path.rstrip(".,!?")  # important

    dir_intent = any(kw in t for kw in [
        "what is in", "what's in", "whats in", "list", "show files", "show me", "contents of", "inside "
    ])
    read_intent = any(kw in t for kw in ["read ", "open ", "show contents", "show me the contents", "print "])
    looks_like_file = bool(re.search(r"\.[a-zA-Z0-9]{1,6}$", path))

    if read_intent and looks_like_file:
        return {"tool": "read_file", "args": {"path": path}, "reason": "User asked to read a file."}
    if dir_intent:
        return {"tool": "list_dir", "args": {"path": path, "max_items": 200}, "reason": "User asked what is inside a folder."}
    return None

# ----------------------------
# Tool result -> human summary
# ----------------------------
def _tool_result_human(tool_name: str, result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return "Tool ran, but returned an unexpected result."
    if not result.get("ok", False):
        return f"{tool_name} failed: {result.get('error','unknown error')}"
    if tool_name == "list_dir":
        items = result.get("items") or []
        path = result.get("path") or ""
        if not items:
            return f"`{path}` is empty."
        dirs = [it.get("name") for it in items if it.get("is_dir")]
        files = [it.get("name") for it in items if not it.get("is_dir")]
        out = [f"Contents of `{path}` ({len(items)} items):"]
        if dirs:
            out.append("\nFolders:")
            for d in dirs[:120]:
                out.append(f"- {d}\\")
            if len(dirs) > 120:
                out.append(f"- …(+{len(dirs)-120} more)")
        if files:
            out.append("\nFiles:")
            for f in files[:200]:
                out.append(f"- {f}")
            if len(files) > 200:
                out.append(f"- …(+{len(files)-200} more)")
        return "\n".join(out)
    if tool_name == "read_file":
        path = result.get("path") or ""
        content = result.get("content") or ""
        if len(content) > 1600:
            content = content[:1600] + "\n…(truncated)"
        return f"Read `{path}`:\n\n{content}"
    if tool_name == "write_file":
        return f"Wrote file: `{result.get('path','')}` ({result.get('bytes_written','?')} bytes)."
    if tool_name == "run_external_process":
        rc = result.get("returncode")
        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        out = [f"Process finished (returncode={rc})."]
        if stdout.strip():
            out.append("\nSTDOUT:\n" + _truncate(stdout, 2000))
        if stderr.strip():
            out.append("\nSTDERR:\n" + _truncate(stderr, 2000))
        return "\n".join(out)
    if tool_name == "apply_patch":
        return f"Patch applied to `{result.get('path','')}` (hunks: {result.get('hunks','?')})."
    return f"{tool_name} completed."

def _looks_like_useless(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    if t in ("done", "done.", "ok", "ok.", "completed", "completed.", "complete", "complete."):
        return True
    if re.fullmatch(r"(done|ok|completed|complete)[.! ]*", t):
        return True
    if len(t) < 10 and all(ch.isalpha() or ch in ".! " for ch in t):
        return True
    return False

def _strip_tool_echoes(text: str) -> str:
    """Remove [Tool:xxx] {...} / [...] blocks the LLM echoes back, handling any nesting depth."""
    result: List[str] = []
    i = 0
    pattern = re.compile(r'\[Tool:\w+\]\s*')
    while i < len(text):
        m = pattern.search(text, i)
        if not m:
            result.append(text[i:])
            break
        result.append(text[i:m.start()])
        i = m.end()
        # Scan past the balanced JSON object/array that follows
        if i < len(text) and text[i] in ('{', '['):
            openers = {'{': '}', '[': ']'}
            closers = set(openers.values())
            depth, in_str, escape = 0, False, False
            while i < len(text):
                c = text[i]
                if escape:
                    escape = False
                elif c == '\\' and in_str:
                    escape = True
                elif c == '"':
                    in_str = not in_str
                elif not in_str:
                    if c in openers:
                        depth += 1
                    elif c in closers:
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                i += 1
            if i < len(text) and text[i] == '\n':
                i += 1
    cleaned = ''.join(result)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

_ACTION_CLAIM_RE = re.compile(
    r"I(?:'ve| have)(?: just| successfully)? (?:created|written|deleted|moved|renamed|made|saved|run|executed)",
    re.IGNORECASE,
)

def _looks_like_action_claim(text: str) -> bool:
    return bool(_ACTION_CLAIM_RE.search(text))

# ----------------------------
# Approval helper
# ----------------------------
def _create_approval(session_id: str, tool: str, args: Dict[str, Any], reason: str, agent_msgs: Optional[List[Dict[str, str]]], auto_voice: bool, diff_preview: Optional[str] = None) -> Dict[str, Any]:
    approval_id = "appr_" + uuid.uuid4().hex[:12]
    PENDING_APPROVALS[approval_id] = {
        "session_id": _normalize_session_id(session_id),
        "tool": tool,
        "args": args,
        "reason": reason,
        "agent_msgs": agent_msgs,
        "auto_voice": bool(auto_voice),
        "created_ts": _now_ts(),
        "diff_preview": diff_preview,
    }
    return {
        "id": approval_id,
        "tool": tool,
        "level": _tool_level(tool),
        "args": args,
        "reason": reason,
        "diff": diff_preview,
    }

# ----------------------------
# Agent loop
# ----------------------------
def _agent_run(session_id: str, auto_voice: bool) -> Dict[str, Any]:
    s = _load_session(session_id)
    messages = s.get("messages", []) or []
    agent_msgs = _build_agent_messages(messages)

    iters = 0
    last_assistant_text = ""
    executed_any_tool = False
    last_tool_name = ""
    last_tool_result: Optional[Dict[str, Any]] = None
    tool_log: List[Dict[str, Any]] = []

    while iters < AGENT_MAX_ITERS:
        iters += 1
        assistant_out = _ollama_chat_retry(agent_msgs)
        tool_call = _try_parse_tool_call(assistant_out)

        if tool_call:
            tool_name = str(tool_call.get("tool") or "").strip()
            args = tool_call.get("args") or {}
            reason = str(tool_call.get("reason") or "").strip()

            if tool_name not in TOOLS:
                last_assistant_text = assistant_out
                break

            if _requires_approval(tool_name):
                approval = _create_approval(
                    session_id=session_id,
                    tool=tool_name,
                    args=args,
                    reason=reason,
                    agent_msgs=agent_msgs,
                    auto_voice=auto_voice,
                    diff_preview=None,
                )
                return {
                    "reply": "",
                    "audio_url": None,
                    "auto_voice": auto_voice,
                    "approval_required": True,
                    "approval": approval,
                }

            executed_any_tool = True
            last_tool_name = tool_name
            result = _execute_tool(tool_name, args)
            last_tool_result = result
            tool_log.append({
                "tool": tool_name,
                "status": "ok" if result.get("ok") is not False else "error",
                "args_preview": ", ".join(f"{k}={str(v)[:50]}" for k, v in list((args or {}).items())[:3]),
            })

            # If propose_patch returns proposed_approval -> create approval for apply_patch immediately
            if tool_name == "propose_patch" and isinstance(result, dict) and result.get("ok") and isinstance(result.get("proposed_approval"), dict):
                pa = result["proposed_approval"]
                proposed_tool = str(pa.get("tool") or "")
                proposed_args = pa.get("args") or {}
                diff_preview = pa.get("diff_preview")
                approval = _create_approval(
                    session_id=session_id,
                    tool=proposed_tool,
                    args=proposed_args,
                    reason=reason or "Patch proposed",
                    agent_msgs=agent_msgs,
                    auto_voice=auto_voice,
                    diff_preview=str(diff_preview) if diff_preview is not None else None,
                )

                # log propose_patch result
                s2 = _load_session(session_id)
                m2 = s2.get("messages", []) or []
                m2.append({"role": "assistant", "content": f"[Tool:{tool_name}] {json.dumps({'ok': True, 'message': result.get('message','')}, ensure_ascii=False)}"})
                s2["messages"] = m2
                _save_session(s2)

                return {
                    "reply": "",
                    "audio_url": None,
                    "auto_voice": auto_voice,
                    "approval_required": True,
                    "approval": approval,
                }

            # store tool trace (truncated)
            safe_result = result
            try:
                safe_result = json.loads(json.dumps(result))
                if isinstance(safe_result, dict):
                    for k in ("stdout", "stderr", "content", "diff"):
                        if k in safe_result and isinstance(safe_result[k], str):
                            safe_result[k] = _truncate(safe_result[k], 4000)
            except Exception:
                safe_result = {"ok": False, "error": "Result serialization failed"}

            s2 = _load_session(session_id)
            m2 = s2.get("messages", []) or []
            m2.append({"role": "assistant", "content": f"[Tool:{tool_name}] {json.dumps(safe_result, ensure_ascii=False)}"})
            s2["messages"] = m2
            _save_session(s2)

            # direct return tools: show deterministic output immediately
            if tool_name in DIRECT_RETURN_TOOLS:
                last_assistant_text = _tool_result_human(tool_name, result)
                break

            # continue tool loop
            agent_msgs.append({"role": "assistant", "content": json.dumps({"tool": tool_name, "args": args, "reason": reason}, ensure_ascii=False)})
            agent_msgs.append({"role": "assistant", "content": f"TOOL_RESULT {tool_name}: {json.dumps(result, ensure_ascii=False)}"})
            continue

        last_assistant_text = assistant_out
        break

    # If model produced useless text after tool(s), fallback to tool result human summary if possible
    if executed_any_tool and (_looks_like_useless(last_assistant_text)):
        if last_tool_name and isinstance(last_tool_result, dict):
            last_assistant_text = _tool_result_human(last_tool_name, last_tool_result)

    last_assistant_text = _strip_tool_echoes(last_assistant_text)

    # Catch hallucinated actions — if the model claims to have done something without calling a tool, reject it
    if not executed_any_tool and _looks_like_action_claim(last_assistant_text):
        last_assistant_text = (
            "I wasn’t able to do that — I need to use a tool to perform file operations, "
            "and I didn’t call one. Please ask again and I’ll use the appropriate tool "
            "(file writes require your approval before anything is changed)."
        )

    if not last_assistant_text.strip():
        last_assistant_text = _force_final_answer(agent_msgs) or "I didn’t get a response from the model. Please try again."

    s = _load_session(session_id)
    msgs = s.get("messages", []) or []
    msgs.append({"role": "assistant", "content": last_assistant_text})

    if not s.get("name") or s.get("name") == _default_session_name(s["id"]):
        for m in msgs:
            if m.get("role") == "user" and (m.get("content") or "").strip():
                s["name"] = _sanitize_name(str(m.get("content") or "")) or s.get("name")
                break

    s["messages"] = msgs
    _save_session(s)

    audio_url = None
    if auto_voice and last_assistant_text.strip():
        audio_url = _synthesize_full(last_assistant_text)

    return {"reply": last_assistant_text, "audio_url": audio_url, "auto_voice": auto_voice, "approval_required": False, "tool_log": tool_log}

def _agent_continue_after_approval(approval_id: str, approved: bool) -> Dict[str, Any]:
    pending = PENDING_APPROVALS.get(approval_id)
    if not pending:
        return {"ok": False, "error": "Approval not found or expired"}

    PENDING_APPROVALS.pop(approval_id, None)

    session_id = _normalize_session_id(str(pending.get("session_id") or "default"))
    tool_name = str(pending.get("tool") or "")
    args = pending.get("args") or {}
    reason = str(pending.get("reason") or "")
    agent_msgs = pending.get("agent_msgs") or []
    auto_voice = bool(pending.get("auto_voice", False))

    if not os.path.exists(_session_path(session_id)):
        return {"ok": False, "error": "Session no longer exists (it may have been burned)."}

    if not approved:
        s = _load_session(session_id)
        msgs = s.get("messages", []) or []
        msgs.append({"role": "assistant", "content": f"Okay — I won’t run `{tool_name}`."})
        s["messages"] = msgs
        _save_session(s)
        return {"ok": True, "reply": "Okay — denied.", "audio_url": None, "approval_required": False}

    result = _execute_tool(tool_name, args)

    # store tool trace (truncated)
    safe_result = result
    try:
        safe_result = json.loads(json.dumps(result))
        if isinstance(safe_result, dict):
            for k in ("stdout", "stderr", "content", "diff"):
                if k in safe_result and isinstance(safe_result[k], str):
                    safe_result[k] = _truncate(safe_result[k], 4000)
    except Exception:
        safe_result = {"ok": False, "error": "Result serialization failed"}

    s = _load_session(session_id)
    msgs = s.get("messages", []) or []
    msgs.append({"role": "assistant", "content": f"[Tool:{tool_name} APPROVED] {json.dumps(safe_result, ensure_ascii=False)}"})

    # direct-return tools: deterministic output immediately
    if tool_name in DIRECT_RETURN_TOOLS:
        human = _tool_result_human(tool_name, result)
        msgs.append({"role": "assistant", "content": human})
        s["messages"] = msgs
        _save_session(s)

        audio_url = None
        if auto_voice and human.strip():
            audio_url = _synthesize_full(human)

        return {"ok": True, "reply": human, "audio_url": audio_url, "approval_required": False, "tool_result": safe_result}

    # apply_patch: deterministic summary (no LLM dependency)
    if tool_name == "apply_patch":
        human = _tool_result_human(tool_name, result)
        msgs.append({"role": "assistant", "content": human})
        s["messages"] = msgs
        _save_session(s)

        audio_url = None
        if auto_voice and human.strip():
            audio_url = _synthesize_full(human)

        return {"ok": True, "reply": human, "audio_url": audio_url, "approval_required": False, "tool_result": safe_result}

    s["messages"] = msgs
    _save_session(s)

    # Continue agent loop for other tools
    agent_msgs.append({"role": "assistant", "content": json.dumps({"tool": tool_name, "args": args, "reason": reason}, ensure_ascii=False)})
    agent_msgs.append({"role": "assistant", "content": f"TOOL_RESULT {tool_name}: {json.dumps(result, ensure_ascii=False)}"})

    assistant_out = _ollama_chat_retry(agent_msgs).strip()
    if not assistant_out:
        assistant_out = _force_final_answer(agent_msgs) or ""

    if _looks_like_useless(assistant_out):
        assistant_out = _tool_result_human(tool_name, result)

    s = _load_session(session_id)
    msgs = s.get("messages", []) or []
    msgs.append({"role": "assistant", "content": assistant_out})
    s["messages"] = msgs
    _save_session(s)

    audio_url = None
    if auto_voice and assistant_out:
        audio_url = _synthesize_full(assistant_out)

    return {"ok": True, "reply": assistant_out, "audio_url": audio_url, "approval_required": False, "tool_result": safe_result}

# ----------------------------
# Warmup
# ----------------------------
def _warmup_worker() -> None:
    global _app_ready, _app_ready_detail
    try:
        _app_ready = False
        _app_ready_detail = "Creating folders..."
        _ensure_dirs()
        _app_ready_detail = "Cleaning temp audio..."
        _cleanup_tmp()
        if KOKORO_PREWARM:
            _app_ready_detail = "Loading Kokoro..."
            _init_tts()
            _app_ready_detail = "Warming voice model..."
        else:
            _app_ready_detail = "Skipping Kokoro prewarm (KOKORO_PREWARM=0)"
        _app_ready_detail = "Ready"
        _app_ready = True
    except Exception as e:
        _set_last_error(e)
        _app_ready_detail = f"Startup error: {e}"
        _app_ready = False

# ----------------------------
# Mount tmp audio
# ----------------------------
_ensure_dirs()
app.mount("/tmp_audio", StaticFiles(directory=TMP_DIR), name="tmp_audio")

# ----------------------------
# API Routes
# ----------------------------
@app.on_event("startup")
def on_startup() -> None:
    global _warm_thread_started, _app_ready, _app_ready_detail
    try:
        _app_ready = False
        _app_ready_detail = "Starting warmup..."
        _ensure_dirs()
        if not _warm_thread_started:
            _warm_thread_started = True
            threading.Thread(target=_warmup_worker, daemon=True).start()
    except Exception as e:
        _set_last_error(e)
        _app_ready_detail = f"Startup error: {e}"
        _app_ready = False

@app.get("/health", response_class=JSONResponse)
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "ready": _app_ready,
        "ready_detail": _app_ready_detail,
        "tts_loaded": bool(_tts_ready),
        "kokoro_voice": KOKORO_VOICE,
        "kokoro_gpu_env": KOKORO_GPU,
        "kokoro_prewarm_env": KOKORO_PREWARM,
        "ollama_url": OLLAMA_CHAT_URL,
        "ollama_model": OLLAMA_MODEL,
        "tmp_dir": TMP_DIR,
        "sessions_dir": SESSIONS_DIR,
        "auto_voice_default_on": AUTO_VOICE_DEFAULT_ON,
        "aegis_mode": AEGIS_MODE,
        "agent_max_iters": AGENT_MAX_ITERS,
        "pending_approvals": len(PENDING_APPROVALS),
        "allowed_roots": ALLOWED_ROOTS,
    }

@app.get("/api/ready", response_class=JSONResponse)
def api_ready() -> Any:
    return {"ready": _app_ready, "detail": _app_ready_detail}

@app.get("/_last_error", response_class=PlainTextResponse)
def last_error() -> str:
    return _last_error or ""

@app.get("/api/tools", response_class=JSONResponse)
def api_tools() -> Any:
    return {"mode": AEGIS_MODE, "tools": {name: {"level": td.level, "description": td.description, "schema": td.schema} for name, td in TOOLS.items()}}

@app.get("/api/sessions", response_class=JSONResponse)
def api_sessions_list() -> Any:
    try:
        return {"sessions": _list_sessions()}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/sessions/new", response_class=JSONResponse)
def api_sessions_new(_: Dict[str, Any] = Body(default={})) -> Any:
    try:
        sid = _new_session_id()
        s = _load_session(sid)
        _save_session(s)
        return {"id": s["id"], "name": s.get("name")}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/sessions/{session_id}", response_class=JSONResponse)
def api_sessions_get(session_id: str) -> Any:
    try:
        s = _load_session(session_id)
        return {"id": s.get("id"), "name": s.get("name"), "created_ts": s.get("created_ts"), "updated_ts": s.get("updated_ts"), "messages": s.get("messages", [])}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/sessions/{session_id}/rename", response_class=JSONResponse)
def api_sessions_rename(session_id: str, payload: Dict[str, Any] = Body(...)) -> Any:
    try:
        new_name = _sanitize_name(str(payload.get("name") or ""))
        if not new_name:
            return JSONResponse(status_code=400, content={"error": "Name cannot be empty"})
        s = _load_session(session_id)
        s["name"] = new_name
        _save_session(s)
        return {"ok": True, "id": s["id"], "name": s["name"]}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/sessions/{session_id}", response_class=JSONResponse)
def api_sessions_delete(session_id: str) -> Any:
    try:
        return _burn_session_everything(session_id)
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/chat", response_class=JSONResponse)
def api_chat(payload: Dict[str, Any] = Body(...)) -> Any:
    if not _app_ready:
        return JSONResponse(status_code=503, content={"error": f"Server not ready: {_app_ready_detail}"})
    try:
        session_id = str(payload.get("session_id") or "").strip() or "default"
        user_text = str(payload.get("user_text") or "").strip()
        auto_voice = bool(payload.get("auto_voice", AUTO_VOICE_DEFAULT_ON))

        if not user_text:
            return {"reply": "", "audio_url": None, "auto_voice": auto_voice, "approval_required": False}


        # ----------------------------
        # Command-mode: direct tool call JSON
        # If the user pastes {"tool": "...", "args": {...}}, execute deterministically
        # and show real approval cards for risky tools (instead of falling through to the LLM).
        # ----------------------------
        direct = _try_parse_tool_call(user_text)
        if direct:
            tool = str(direct.get("tool") or "").strip()
            args = direct.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            reason = str(direct.get("reason") or "Direct tool call").strip()

            # persist the user's message
            session = _load_session(session_id)
            msgs = session.get("messages", []) or []
            msgs.append({"role": "user", "content": user_text})
            session["messages"] = msgs
            _save_session(session)

            if tool not in TOOLS:
                reply = json.dumps({"ok": False, "error": f"Unknown tool: {tool}"}, ensure_ascii=False, indent=2)
                msgs.append({"role": "assistant", "content": reply})
                session["messages"] = msgs
                _save_session(session)
                return {"reply": reply, "audio_url": None, "auto_voice": auto_voice, "approval_required": False}

            # risky tools always require approval in semi mode
            if _requires_approval(tool):
                approval = _create_approval(
                    session_id=session["id"],
                    tool=tool,
                    args=args,
                    reason=reason,
                    agent_msgs=None,
                    auto_voice=auto_voice,
                    diff_preview=None,
                )
                return {"reply": "", "audio_url": None, "auto_voice": auto_voice, "approval_required": True, "approval": approval}

            # execute SAFE tool immediately
            result = _execute_tool(tool, args)

            # Special: propose_patch returns proposed_approval for apply_patch (risky) -> show approval card
            proposed = None
            if isinstance(result, dict):
                proposed = result.get("proposed_approval")

            if proposed and isinstance(proposed, dict):
                p_tool = str(proposed.get("tool") or "").strip()
                p_args = proposed.get("args") or {}
                p_preview = proposed.get("diff_preview")
                if not isinstance(p_args, dict):
                    p_args = {}
                if p_tool and _requires_approval(p_tool):
                    approval = _create_approval(
                        session_id=session["id"],
                        tool=p_tool,
                        args=p_args,
                        reason=(result.get("message") or "Approval required"),
                        agent_msgs=None,
                        auto_voice=auto_voice,
                        diff_preview=p_preview if isinstance(p_preview, str) else None,
                    )
                    msgs.append({"role": "assistant", "content": "Patch validated. Approval required to apply."})
                    session["messages"] = msgs
                    _save_session(session)
                    return {"reply": "", "audio_url": None, "auto_voice": auto_voice, "approval_required": True, "approval": approval}

            reply = json.dumps(result, ensure_ascii=False, indent=2)
            msgs.append({"role": "assistant", "content": reply})
            session["messages"] = msgs
            _save_session(session)
            return {"reply": reply, "audio_url": None, "auto_voice": auto_voice, "approval_required": False}

        heuristic = _heuristic_tool_suggestion(user_text)
        if heuristic and _requires_approval(heuristic["tool"]):
            approval = _create_approval(
                session_id=session_id,
                tool=heuristic["tool"],
                args=heuristic["args"],
                reason=heuristic.get("reason", "Heuristic suggestion"),
                agent_msgs=None,
                auto_voice=auto_voice,
                diff_preview=None,
            )

            session = _load_session(session_id)
            msgs = session.get("messages", []) or []
            msgs.append({"role": "user", "content": user_text})
            session["messages"] = msgs
            _save_session(session)

            return {
                "reply": "",
                "audio_url": None,
                "auto_voice": auto_voice,
                "approval_required": True,
                "approval": approval,
            }

        session = _load_session(session_id)
        msgs = session.get("messages", []) or []
        msgs.append({"role": "user", "content": user_text})
        session["messages"] = msgs
        _save_session(session)

        out = _agent_run(session_id=session["id"], auto_voice=auto_voice)
        return out
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/approval/respond", response_class=JSONResponse)
def api_approval_respond(payload: Dict[str, Any] = Body(...)) -> Any:
    if not _app_ready:
        return JSONResponse(status_code=503, content={"error": f"Server not ready: {_app_ready_detail}"})
    try:
        approval_id = str(payload.get("approval_id") or "").strip()
        approved = bool(payload.get("approved", False))
        if not approval_id:
            return JSONResponse(status_code=400, content={"error": "approval_id required"})

        out = _agent_continue_after_approval(approval_id, approved)
        if not out.get("ok"):
            return JSONResponse(status_code=404, content={"error": out.get("error", "not found")})
        return {"reply": out.get("reply", ""), "audio_url": out.get("audio_url"), "approval_required": False}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/tts", response_class=JSONResponse)
def api_tts(payload: Dict[str, Any] = Body(...)) -> Any:
    if not _app_ready:
        return JSONResponse(status_code=503, content={"error": f"Server not ready: {_app_ready_detail}"})
    try:
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"audio_url": None}
        audio_url = _synthesize_full(text)
        return {"audio_url": audio_url}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

_VALID_VOICES = {
    "af_heart": "Heart — American Female",
    "af_bella": "Bella — American Female",
    "am_adam":  "Adam — American Male",
    "bf_emma":  "Emma — British Female",
    "bm_george":"George — British Male",
}

@app.get("/api/settings", response_class=JSONResponse)
def api_settings_get() -> Any:
    return {
        "voice": _active_voice,
        "available_voices": [{"id": k, "label": v} for k, v in _VALID_VOICES.items()],
    }

@app.post("/api/settings", response_class=JSONResponse)
def api_settings_post(payload: Dict[str, Any] = Body(...)) -> Any:
    global _active_voice
    voice = str(payload.get("voice", "")).strip()
    if voice not in _VALID_VOICES:
        return JSONResponse(status_code=400, content={"error": f"Invalid voice: {voice}"})
    _active_voice = voice
    _save_prefs({**_load_prefs(), "voice": voice})
    return {"ok": True, "voice": _active_voice}

def _init_stt() -> None:
    global _stt_model, _stt_ready
    if _stt_ready:
        return
    from faster_whisper import WhisperModel
    _stt_model = WhisperModel("small.en", device="cpu", compute_type="int8")
    _stt_ready = True

@app.post("/api/stt", response_class=JSONResponse)
async def api_stt(audio: UploadFile = File(...)) -> Any:
    if not _app_ready:
        return JSONResponse(status_code=503, content={"error": "Server not ready"})
    try:
        _init_stt()
        import tempfile
        suffix = ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir=TMP_DIR) as tmp:
            tmp.write(await audio.read())
            tmp_path = tmp.name
        try:
            segments, _ = _stt_model.transcribe(tmp_path, language="en")
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return {"text": text}
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/tools/mcp", response_class=JSONResponse)
def api_tools_mcp() -> Any:
    """Return tool schemas in Model Context Protocol (MCP) format."""
    return {"tools": get_mcp_tools(TOOLS)}

@app.post("/api/eval", response_class=JSONResponse)
def api_eval() -> Any:
    """Run the eval harness and return scored results."""
    if not _app_ready:
        return JSONResponse(status_code=503, content={"error": "Server not ready"})
    try:
        from evals.runner import run_evals
        results = run_evals(TOOLS, SESSIONS_DIR)
        return results
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/last_assistant", response_class=JSONResponse)
def api_last_assistant(session_id: str) -> Any:
    try:
        session = _load_session(session_id)
        msgs = session.get("messages", [])
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                return {"text": m.get("content", "")}
        return {"text": ""}
    except Exception as e:
        _set_last_error(e)
        return JSONResponse(status_code=500, content={"error": str(e)})

# ----------------------------
# UI
# ----------------------------
@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{APP_TITLE}</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%231f6feb'/%3E%3Ctext x='16' y='23' font-family='system-ui' font-size='20' font-weight='800' text-anchor='middle' fill='white'%3EA%3C/text%3E%3C/svg%3E" />
  <style>
    :root {{
      --bg: #0b0f14;
      --panel: #2a2f36;
      --card:  #2a2f36;
      --border: #3b4350;
      --text: #e6edf3;
      --muted: rgba(230,237,243,0.75);
      --user: #1c2a3d;

      --assistant: #4b2a12;
      --assistantBorder: #ff9a2f;

      --btn: #1f6feb;
      --btn2: #3a4250;
      --hover: rgba(255,255,255,0.06);
      --active: rgba(31,111,235,0.18);
      --field: #1e232a;

      --warnBg: rgba(255, 170, 0, 0.10);
      --warnBorder: rgba(255, 170, 0, 0.35);
    }}
    html, body {{
      height: 100%;
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, Arial;
      overflow: hidden;
    }}
    .app {{
      height: 100%;
      display: grid;
      grid-template-columns: var(--sidebar-w, 320px) 8px 1fr;
    }}
    .sidebar {{
      background: var(--panel);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      min-width: 220px;
      overflow: hidden;
      position: relative;
    }}
    .sidebarFooter {{
      padding: 8px 12px;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: flex-start;
      flex-shrink: 0;
    }}
    .cogBtn {{
      width: 32px;
      height: 32px;
      border-radius: 10px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 16px;
      transition: background 120ms ease, transform 200ms ease;
    }}
    .cogBtn:hover {{ background: rgba(255,255,255,0.12); transform: rotate(30deg); }}
    .settingsPanel {{
      position: absolute;
      bottom: 52px;
      left: 10px;
      right: 10px;
      background: #1f242b;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.45);
      display: none;
      flex-direction: column;
      gap: 12px;
      z-index: 100;
    }}
    .settingsPanel.show {{ display: flex; }}
    .settingsPanel .settingsLabel {{
      font-size: 11px;
      color: var(--muted);
      margin-bottom: 4px;
      display: block;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .settingsPanel select {{
      width: 100%;
      background: var(--field);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px;
      box-sizing: border-box;
      font-size: 13px;
      cursor: pointer;
    }}
    .resizer {{
      cursor: col-resize;
      background: rgba(255,255,255,0.03);
      border-right: 1px solid var(--border);
    }}
    .resizer:hover {{ background: rgba(31,111,235,0.18); }}
    .sidebarHeader {{
      padding: 12px;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .row {{
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .small {{ opacity: 0.8; font-size: 12px; color: var(--muted); }}
    button {{
      background: var(--btn);
      color: white;
      border: 0;
      border-radius: 10px;
      padding: 10px 12px;
      cursor: pointer;
    }}
    button.secondary {{ background: var(--btn2); }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    input[type="text"] {{
      width: 100%;
      background: var(--field);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      box-sizing: border-box;
    }}
    .sessions {{
      flex: 1;
      overflow-y: auto;
      padding: 8px;
    }}
    .sess {{
      padding: 10px 10px;
      border: 1px solid var(--border);
      border-radius: 10px;
      margin: 8px 0;
      cursor: pointer;
      background: rgba(0,0,0,0.12);
      user-select: none;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }}
    .sess:hover {{ background: var(--hover); }}
    .sess.active {{ background: var(--active); border-color: rgba(31,111,235,0.55); }}
    .sessMain {{ min-width: 0; flex: 1; }}
    .sessTitle {{
      font-size: 14px;
      font-weight: 600;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .sessMeta {{
      margin-top: 4px;
      font-size: 12px;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
      gap: 8px;
    }}
    .sessActions {{
      display: flex;
      gap: 6px;
      flex-shrink: 0;
      margin-top: 1px;
    }}
    .iconBtn {{
      width: 28px;
      height: 28px;
      border-radius: 10px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
    }}
    .iconBtn:hover {{ background: rgba(255,255,255,0.10); }}
    .burnIcon {{ font-size: 14px; opacity: 0.9; }}
    .hint {{ font-size: 11px; color: var(--muted); }}

    .main {{
      height: 100%;
      display: grid;
      grid-template-rows: auto 1fr auto;
      padding: 14px;
      box-sizing: border-box;
      gap: 12px;
      overflow: hidden;
      max-width: 1200px;
    }}
    .topbar {{
      display: flex;
      gap: 14px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .brand {{
      font-weight: 800;
      letter-spacing: 0.2px;
      font-size: 15px;
      display: inline-flex;
      align-items: baseline;
      gap: 8px;
    }}
    .brand .ver {{
      font-weight: 700;
      font-size: 12px;
      opacity: 0.75;
    }}
    .sessionTitle {{
      font-weight: 700;
      font-size: 13px;
      opacity: 0.9;
    }}
    .chat {{
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-right: 6px;
      overscroll-behavior: contain;
    }}
    .bubble {{
      padding: 10px 12px;
      border-radius: 12px;
      line-height: 1.35;
      white-space: pre-wrap;
      border: 1px solid var(--border);
      max-width: 92%;
    }}
    .user {{
      background: var(--user);
      align-self: flex-end;
      border-color: #2c405c;
    }}
    .assistant {{
      background: var(--assistant);
      align-self: flex-start;
      border-color: var(--assistantBorder);
    }}
    .approval {{
      background: var(--warnBg);
      border-color: var(--warnBorder);
    }}
    .approvalTitle {{ font-weight: 700; margin-bottom: 6px; }}
    .approvalKV {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 12px;
      opacity: 0.9;
      white-space: pre-wrap;
    }}
    .diffBox {{
      margin-top: 10px;
      border: 1px solid rgba(255,255,255,0.16);
      background: rgba(0,0,0,0.25);
      border-radius: 10px;
      padding: 10px;
      max-height: 320px;
      overflow: auto;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 12px;
      white-space: pre;
      line-height: 1.35;
    }}
    .approvalBtns {{
      display: flex;
      gap: 10px;
      margin-top: 10px;
    }}
    .composer {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
    }}
    textarea {{
      width: 100%;
      min-height: 70px;
      resize: vertical;
      background: var(--field);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      box-sizing: border-box;
    }}
    audio {{ display: none; }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.72);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 9999;
    }}
    .overlay.show {{ display: flex; }}
    .overlayCard {{
      width: min(520px, 92vw);
      background: #1f242b;
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.35);
    }}
    .spinner {{
      width: 34px;
      height: 34px;
      border-radius: 50%;
      border: 4px solid rgba(255,255,255,0.18);
      border-top-color: rgba(31,111,235,0.95);
      animation: spin 1s linear infinite;
      margin-right: 12px;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .toggle {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      user-select: none;
    }}
    .switch {{
      width: 44px;
      height: 24px;
      border-radius: 999px;
      background: rgba(255,255,255,0.18);
      border: 1px solid rgba(255,255,255,0.22);
      position: relative;
      cursor: pointer;
    }}
    .switch.on {{
      background: rgba(31,111,235,0.55);
      border-color: rgba(31,111,235,0.7);
    }}
    .knob {{
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: rgba(255,255,255,0.9);
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      left: 3px;
      transition: left 120ms ease;
    }}
    .switch.on .knob {{ left: 23px; }}

    /* Mic button */
    #micBtn {{
      transition: background 120ms, box-shadow 120ms;
    }}
    #micBtn.recording {{
      background: rgba(218,54,51,0.75);
      box-shadow: 0 0 0 3px rgba(218,54,51,0.3);
      animation: micpulse 1.2s ease-in-out infinite;
    }}
    @keyframes micpulse {{
      0%, 100% {{ box-shadow: 0 0 0 3px rgba(218,54,51,0.3); }}
      50%  {{ box-shadow: 0 0 0 7px rgba(218,54,51,0.08); }}
    }}

    /* Voice Orb */
    #voiceOrbWrap {{
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 10px 0 8px;
      width: 100%;
      background: transparent;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }}
    #voiceOrbContainer {{
      position: relative;
      width: 150px;
      height: 150px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    #orbCanvas {{
      position: absolute;
      top: 0; left: 0;
      pointer-events: none;
      z-index: 0;
      background: transparent;
    }}
    #voiceOrb {{
      width: 64px;
      height: 64px;
      border-radius: 50%;
      position: relative;
      z-index: 1;
      background: radial-gradient(circle at 30% 30%, rgba(255,165,70,0.80), rgba(200,75,10,0.88) 55%, rgba(55,18,3,0.92));
      box-shadow: 0 0 16px 5px rgba(210,100,20,0.32);
      animation: orbIdle 3s ease-in-out infinite;
      transition: background 0.4s ease, box-shadow 0.4s ease;
    }}
    #voiceOrb.user-speaking {{
      background: radial-gradient(circle at 35% 35%, #60b4ff, #1a6fd4 55%, #0a3a7a);
      box-shadow: 0 0 24px 8px rgba(60,140,255,0.45), 0 0 44px 14px rgba(30,100,220,0.18);
      animation: orbPulse 0.9s ease-in-out infinite;
    }}
    #voiceOrb.llm-speaking {{
      background: radial-gradient(circle at 35% 35%, #ffcc66, #e87c20 55%, #7a3600);
      box-shadow: 0 0 24px 8px rgba(240,140,40,0.50), 0 0 44px 14px rgba(200,100,20,0.20);
      animation: orbPulse 1.1s ease-in-out infinite;
    }}
    @keyframes orbIdle {{
      0%, 100% {{ transform: scale(1);    box-shadow: 0 0 16px 5px rgba(210,100,20,0.30); }}
      50%       {{ transform: scale(1.04); box-shadow: 0 0 24px 8px rgba(210,100,20,0.45); }}
    }}
    @keyframes orbPulse {{
      0%, 100% {{ transform: scale(1);    }}
      50%       {{ transform: scale(1.10); }}
    }}

    /* Waveform speaking indicator */
    .waveform {{
      display: none;
      align-items: flex-end;
      gap: 2px;
      height: 18px;
    }}
    .waveform.speaking {{ display: flex; }}
    .waveform span {{
      width: 3px;
      border-radius: 2px;
      background: rgba(31,111,235,0.85);
      height: 4px;
      animation: wavebar 0.9s ease-in-out infinite;
    }}
    .waveform span:nth-child(1) {{ animation-delay: 0.00s; }}
    .waveform span:nth-child(2) {{ animation-delay: 0.15s; }}
    .waveform span:nth-child(3) {{ animation-delay: 0.30s; }}
    .waveform span:nth-child(4) {{ animation-delay: 0.45s; }}
    .waveform span:nth-child(5) {{ animation-delay: 0.60s; }}
    @keyframes wavebar {{
      0%, 100% {{ height: 4px; opacity: 0.5; }}
      50% {{ height: 16px; opacity: 1; }}
    }}

    /* Tool call timeline */
    .toolTimeline {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      margin-bottom: 4px;
    }}
    .toolCard {{
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      font-size: 12px;
      background: rgba(0,0,0,0.18);
    }}
    .toolCardHeader {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 5px 10px;
      cursor: pointer;
      user-select: none;
    }}
    .toolCardHeader:hover {{ background: rgba(255,255,255,0.04); }}
    .toolName {{ font-weight: 600; font-family: monospace; }}
    .toolArgs {{ color: var(--muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .toolBadge {{
      font-size: 10px;
      padding: 2px 7px;
      border-radius: 4px;
      font-weight: 700;
      flex-shrink: 0;
    }}
    .toolBadge.ok {{ background: rgba(35,134,54,0.3); color: #3fb950; }}
    .toolBadge.error {{ background: rgba(248,81,73,0.2); color: #f85149; }}
    .toolCardChevron {{ font-size: 10px; color: var(--muted); margin-left: 2px; transition: transform 120ms; }}
    .toolCard.open .toolCardChevron {{ transform: rotate(90deg); }}
    .toolCardBody {{
      padding: 6px 10px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-family: monospace;
      font-size: 11px;
      display: none;
    }}
    .toolCard.open .toolCardBody {{ display: block; }}
  </style>
</head>
<body>

<div id="overlay" class="overlay">
  <div class="overlayCard">
    <div class="row" style="align-items:center;">
      <div class="spinner"></div>
      <div>
        <div style="font-weight:700; font-size:16px;">Warming up…</div>
        <div class="small" id="warmDetail">Starting…</div>
      </div>
    </div>
    <div class="small" style="margin-top:10px;">You can view the UI, but it will unlock when models are ready.</div>
  </div>
</div>

<div class="app" id="appRoot">
  <div class="sidebar">
    <div id="voiceOrbWrap">
      <div id="voiceOrbContainer">
        <canvas id="orbCanvas" width="150" height="150"></canvas>
        <div id="voiceOrb"></div>
      </div>
    </div>
    <div class="sidebarHeader">
      <div class="row" style="justify-content: space-between;">
        <strong>Sessions</strong>
        <button class="secondary" id="newSessionBtn">New</button>
      </div>
      <input id="sessionSearch" type="text" placeholder="Search sessions..." />
      <div class="hint">Tip: Right-click a session to rename • Click 🔥 to burn (delete)</div>
    </div>
    <div class="sessions" id="sessions"></div>
    <div class="settingsPanel" id="settingsPanel">
      <div>
        <span class="settingsLabel">Voice</span>
        <select id="voiceSelect">
          <option value="af_heart">Heart — American Female</option>
          <option value="af_bella">Bella — American Female</option>
          <option value="am_adam">Adam — American Male</option>
          <option value="bf_emma">Emma — British Female</option>
          <option value="bm_george">George — British Male</option>
        </select>
      </div>
    </div>
    <div class="sidebarFooter">
      <button class="cogBtn" id="cogBtn" title="Settings">⚙</button>
    </div>
  </div>

  <div class="resizer" id="resizer"></div>

  <div class="main">
    <div class="topbar">
      <div class="brand">
        <span id="brandName">{APP_NAME}</span>
        <span class="ver" id="brandVer">{APP_VERSION}</span>
      </div>

      <span class="small">Auto Voice:</span>
      <div class="toggle" title="Toggle Auto Voice">
        <div id="autoVoiceSwitch" class="switch"><div class="knob"></div></div>
        <span class="small" id="autoVoiceLabel"></span>
      </div>

      <div class="waveform" id="waveform">
        <span></span><span></span><span></span><span></span><span></span>
      </div>
      <span class="sessionTitle" id="sessionTitle"></span>
      <span class="small" id="status"></span>
    </div>

    <div class="chat" id="chat"></div>

    <div class="composer">
      <div style="margin-top:0px;">
        <textarea id="msg" placeholder="Type... (Enter to send • Shift+Enter for newline)"></textarea>
      </div>
      <div class="row" style="margin-top:10px; justify-content:flex-end;">
        <button class="secondary" id="micBtn" title="Toggle voice input">🎤</button>
        <button class="secondary" id="voiceLast">Voice Last</button>
        <button id="send">Send</button>
      </div>
      <audio id="player"></audio>
    </div>
  </div>
</div>

<script>
const UI_MAX_BUBBLES = {UI_MAX_BUBBLES};
const AUTO_VOICE_DEFAULT_ON = {str(AUTO_VOICE_DEFAULT_ON).lower()};
const APP_NAME = {json.dumps(APP_NAME)};
const APP_VERSION = {json.dumps(APP_VERSION)};

const overlay = document.getElementById("overlay");
const warmDetail = document.getElementById("warmDetail");
const appRoot = document.getElementById("appRoot");

function setLocked(locked) {{
  if (locked) {{
    overlay.classList.add("show");
    appRoot.style.pointerEvents = "none";
    appRoot.style.filter = "blur(0.6px)";
  }} else {{
    overlay.classList.remove("show");
    appRoot.style.pointerEvents = "auto";
    appRoot.style.filter = "none";
  }}
}}

async function pollReadyOnce() {{
  try {{
    const r = await fetch("/api/ready");
    const j = await r.json();
    warmDetail.textContent = j.detail || "Starting…";
    if (j.ready) {{
      setLocked(false);
      return true;
    }} else {{
      setLocked(true);
      return false;
    }}
  }} catch (e) {{
    warmDetail.textContent = "Connecting…";
    setLocked(true);
    return false;
  }}
}}

(async () => {{
  await pollReadyOnce();
  while (true) {{
    const ok = await pollReadyOnce();
    if (ok) break;
    await new Promise(res => setTimeout(res, 250));
  }}
}})();

const resizer = document.getElementById("resizer");
let isResizing = false;
const savedW = localStorage.getItem("director_sidebar_w");
if (savedW) {{
  document.documentElement.style.setProperty("--sidebar-w", savedW + "px");
}}
resizer.addEventListener("mousedown", (e) => {{
  isResizing = true;
  document.body.style.cursor = "col-resize";
  e.preventDefault();
}});
window.addEventListener("mousemove", (e) => {{
  if (!isResizing) return;
  const minW = 220;
  const maxW = 520;
  const w = Math.max(minW, Math.min(maxW, e.clientX));
  document.documentElement.style.setProperty("--sidebar-w", w + "px");
  localStorage.setItem("director_sidebar_w", String(w));
}});
window.addEventListener("mouseup", () => {{
  if (!isResizing) return;
  isResizing = false;
  document.body.style.cursor = "default";
}});

const autoVoiceSwitch = document.getElementById("autoVoiceSwitch");
const autoVoiceLabel = document.getElementById("autoVoiceLabel");
function getAutoVoice() {{
  const v = localStorage.getItem("director_auto_voice");
  if (v === null) return !!AUTO_VOICE_DEFAULT_ON;
  return v === "1";
}}
function setAutoVoice(val) {{
  localStorage.setItem("director_auto_voice", val ? "1" : "0");
  renderAutoVoice();
}}
function renderAutoVoice() {{
  const on = getAutoVoice();
  autoVoiceSwitch.classList.toggle("on", on);
  autoVoiceLabel.textContent = on ? "ON" : "OFF";
}}
autoVoiceSwitch.addEventListener("click", () => setAutoVoice(!getAutoVoice()));
renderAutoVoice();

// Settings panel
const cogBtn = document.getElementById("cogBtn");
const settingsPanel = document.getElementById("settingsPanel");
const voiceSelect = document.getElementById("voiceSelect");

cogBtn.addEventListener("click", (e) => {{
  e.stopPropagation();
  settingsPanel.classList.toggle("show");
}});
document.addEventListener("click", () => settingsPanel.classList.remove("show"));
settingsPanel.addEventListener("click", (e) => e.stopPropagation());

async function loadSettings() {{
  try {{
    const r = await fetch("/api/settings");
    const d = await r.json();
    voiceSelect.value = d.voice;
  }} catch {{}}
}}

voiceSelect.addEventListener("change", async () => {{
  try {{
    await fetch("/api/settings", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{voice: voiceSelect.value}})
    }});
  }} catch {{}}
}});

loadSettings();

let sessionId = localStorage.getItem("director_session_id");
if (!sessionId) {{
  sessionId = "sess_" + Math.random().toString(16).slice(2);
  localStorage.setItem("director_session_id", sessionId);
}}

const sessionsEl = document.getElementById("sessions");
const searchEl = document.getElementById("sessionSearch");
const chatEl = document.getElementById("chat");
const msgEl = document.getElementById("msg");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("status");
const player = document.getElementById("player");
const sessionTitleEl = document.getElementById("sessionTitle");

let stickToBottom = true;
chatEl.addEventListener("scroll", () => {{
  const threshold = 48;
  const atBottom = (chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight) < threshold;
  stickToBottom = atBottom;
}});
function scrollToBottomIfNeeded(force=false) {{
  if (force || stickToBottom) chatEl.scrollTop = chatEl.scrollHeight;
}}

function fmtTime(ts) {{
  if (!ts) return "";
  try {{
    const d = new Date(ts * 1000);
    return d.toLocaleString();
  }} catch {{
    return "";
  }}
}}

function addBubble(role, text) {{
  const div = document.createElement("div");
  div.className = "bubble " + (role === "user" ? "user" : "assistant");
  div.textContent = text;
  chatEl.appendChild(div);

  while (chatEl.children.length > UI_MAX_BUBBLES) {{
    chatEl.removeChild(chatEl.firstChild);
  }}
  scrollToBottomIfNeeded();
}}

function addApprovalBubble(approval) {{
  const div = document.createElement("div");
  div.className = "bubble assistant approval";

  const title = document.createElement("div");
  title.className = "approvalTitle";
  title.textContent = "Approval required (risky tool)";

  const kv = document.createElement("div");
  kv.className = "approvalKV";
  kv.textContent =
    "tool: " + approval.tool + "\\n" +
    "level: " + approval.level + "\\n" +
    "reason: " + (approval.reason || "") + "\\n" +
    "args: " + JSON.stringify(approval.args || {{}}, null, 2);

  div.appendChild(title);
  div.appendChild(kv);

  if (approval.diff) {{
    const diffBox = document.createElement("div");
    diffBox.className = "diffBox";
    diffBox.textContent = approval.diff;
    div.appendChild(diffBox);
  }}

  const btns = document.createElement("div");
  btns.className = "approvalBtns";

  const approveBtn = document.createElement("button");
  approveBtn.textContent = "Approve";

  const denyBtn = document.createElement("button");
  denyBtn.textContent = "Deny";
  denyBtn.className = "secondary";

  btns.appendChild(denyBtn);
  btns.appendChild(approveBtn);
  div.appendChild(btns);

  chatEl.appendChild(div);
  scrollToBottomIfNeeded(true);

  async function respond(approved) {{
    approveBtn.disabled = true;
    denyBtn.disabled = true;
    statusEl.textContent = approved ? "Approving..." : "Denying...";
    try {{
      const res = await apiPost("/api/approval/respond", {{
        approval_id: approval.id,
        approved: !!approved,
        session_id: sessionId,
        auto_voice: getAutoVoice()
      }});
      div.remove();
      if (res.reply) addBubble("assistant", res.reply);
      if (res.audio_url) {{
        statusEl.textContent = "Voicing...";
        playUrl(res.audio_url);
      }}
      statusEl.textContent = "";
      await refreshSessions();
    }} catch (e) {{
      statusEl.textContent = "Error: " + e.message;
      approveBtn.disabled = false;
      denyBtn.disabled = false;
    }}
  }}

  approveBtn.addEventListener("click", () => respond(true));
  denyBtn.addEventListener("click", () => respond(false));
}}

function clearChat() {{
  chatEl.innerHTML = "";
}}

const waveform = document.getElementById("waveform");
const voiceOrbWrap = document.getElementById("voiceOrbWrap");
const voiceOrb = document.getElementById("voiceOrb");
const orbCanvas = document.getElementById("orbCanvas");
const orbCtx = orbCanvas.getContext("2d");

// ---- Orb particle system ----
const _ORB_R = 32, _CANVAS_SZ = 150;
let _orbParticles = [], _orbAnimFrame = null, _orbState = "idle";

function _makeParticle(state) {{
  const idle = state === "idle";
  return {{
    angle: Math.random() * Math.PI * 2,
    radius: idle ? _ORB_R + 8 + Math.random() * 16 : _ORB_R + 10 + Math.random() * 28,
    speed: (idle ? 0.004 + Math.random() * 0.006 : 0.011 + Math.random() * 0.022) * (Math.random() < 0.5 ? 1 : -1),
    size: idle ? 1.0 + Math.random() * 1.4 : 1.4 + Math.random() * 2.4,
    opacity: idle ? 0.15 + Math.random() * 0.22 : 0.35 + Math.random() * 0.55,
    drift: (Math.random() - 0.5) * (idle ? 0.001 : 0.005),
  }};
}}

function _spawnParticles(state) {{
  const count = state === "idle" ? 20 : 32;
  _orbParticles = Array.from({{ length: count }}, () => _makeParticle(state));
}}

function _orbDraw() {{
  orbCtx.clearRect(0, 0, _CANVAS_SZ, _CANVAS_SZ);
  const cx = _CANVAS_SZ / 2, cy = _CANVAS_SZ / 2;
  const col = _orbState === "user" ? "rgba(80,160,255," : _orbState === "llm" ? "rgba(240,140,40," : "rgba(220,110,20,";
  const minR = _ORB_R + 8;
  const maxR = _orbState === "idle" ? _ORB_R + 24 : _ORB_R + 40;
  for (const p of _orbParticles) {{
    p.angle += p.speed;
    p.radius += p.drift;
    if (p.radius < minR) p.radius = minR;
    if (p.radius > maxR) p.radius = maxR;
    const x = cx + Math.cos(p.angle) * p.radius;
    const y = cy + Math.sin(p.angle) * p.radius;
    orbCtx.beginPath();
    orbCtx.arc(x, y, p.size, 0, Math.PI * 2);
    orbCtx.fillStyle = col + p.opacity + ")";
    orbCtx.fill();
  }}
  _orbAnimFrame = requestAnimationFrame(_orbDraw);
}}

function _startParticles(state) {{
  const changed = _orbState !== state;
  _orbState = state;
  if (!_orbParticles.length) {{
    _spawnParticles(state);
  }} else if (changed) {{
    const idle = state === "idle";
    for (const p of _orbParticles) {{
      p.speed = (idle ? 0.004 + Math.random() * 0.006 : 0.011 + Math.random() * 0.022) * (p.speed < 0 ? -1 : 1);
      p.drift = (Math.random() - 0.5) * (idle ? 0.001 : 0.005);
      p.size  = idle ? 1.0 + Math.random() * 1.4 : 1.4 + Math.random() * 2.4;
      p.opacity = idle ? 0.15 + Math.random() * 0.22 : 0.35 + Math.random() * 0.55;
    }}
    if (!idle) {{
      while (_orbParticles.length < 32) _orbParticles.push(_makeParticle(state));
    }}
  }}
  if (!_orbAnimFrame) _orbDraw();
}}

function _stopParticles() {{
  _orbState = "idle";
  if (_orbAnimFrame) {{ cancelAnimationFrame(_orbAnimFrame); _orbAnimFrame = null; }}
  orbCtx.clearRect(0, 0, _CANVAS_SZ, _CANVAS_SZ);
  _orbParticles = [];
}}

// Orb always visible — start idle particles immediately on load
_startParticles("idle");

let _isTTSPlaying = false;
player.addEventListener("play",  () => {{ waveform.classList.add("speaking"); _isTTSPlaying = true; voiceOrb.classList.remove("user-speaking"); voiceOrb.classList.add("llm-speaking"); _startParticles("llm"); }});
player.addEventListener("ended", () => {{ waveform.classList.remove("speaking"); _isTTSPlaying = false; voiceOrb.classList.remove("llm-speaking"); _startParticles("idle"); _processQueuedSpeech(); }});
player.addEventListener("pause", () => {{ waveform.classList.remove("speaking"); _isTTSPlaying = false; voiceOrb.classList.remove("llm-speaking"); _startParticles("idle"); }});

function playUrl(url) {{
  player.pause();
  player.src = url;
  player.currentTime = 0;
  player.play().catch(()=>{{}});
}}

// ---- Voice Input (Mic) — VAD mode ----
const micBtn = document.getElementById("micBtn");
let _voiceModeOn  = false;
let _audioCtx     = null, _mediaStream = null, _scriptProc = null, _analyser = null;
let _micChunks    = [], _isSpeechActive = false;
let _speechStartTime = null, _silenceStartTime = null;
let _vadInterval  = null, _queuedChunks = null;

const SILENCE_THRESHOLD = 0.025;  // RMS below this = silence
const SILENCE_SEND_MS   = 1200;   // ms of silence before auto-send
const MIN_SPEECH_MS     = 400;    // minimum speech duration to bother sending

function _statusReset() {{
  statusEl.textContent = _voiceModeOn ? "Listening..." : "";
}}

function _encodeWAV(samples, sampleRate) {{
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const ws = (o, s) => {{ for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }};
  ws(0,"RIFF"); v.setUint32(4, 36 + samples.length * 2, true);
  ws(8,"WAVE"); ws(12,"fmt ");
  v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,1,true);
  v.setUint32(24,sampleRate,true); v.setUint32(28,sampleRate*2,true);
  v.setUint16(32,2,true); v.setUint16(34,16,true);
  ws(36,"data"); v.setUint32(40,samples.length*2,true);
  let o = 44;
  for (let i = 0; i < samples.length; i++) {{
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true); o += 2;
  }}
  return new Blob([buf], {{ type: "audio/wav" }});
}}

function _getRMS() {{
  if (!_analyser) return 0;
  const data = new Uint8Array(_analyser.fftSize);
  _analyser.getByteTimeDomainData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i++) {{ const v = (data[i] - 128) / 128; sum += v * v; }}
  return Math.sqrt(sum / data.length);
}}

async function _transcribeAndSend(chunks) {{
  const total = chunks.reduce((n, c) => n + c.length, 0);
  if (!total) return;
  const pcm = new Float32Array(total);
  let off = 0;
  for (const c of chunks) {{ pcm.set(c, off); off += c.length; }}
  statusEl.textContent = "Transcribing...";
  try {{
    const fd = new FormData();
    fd.append("audio", _encodeWAV(pcm, 16000), "recording.wav");
    const r = await fetch("/api/stt", {{ method: "POST", body: fd }});
    const d = await r.json();
    if (d.text && d.text.trim()) {{
      msgEl.value = d.text;
      await doSend();
    }} else {{
      _statusReset();
    }}
  }} catch(e) {{
    statusEl.textContent = "STT error: " + e.message;
    setTimeout(_statusReset, 2500);
  }}
}}

async function _processQueuedSpeech() {{
  if (!_queuedChunks || !_voiceModeOn) return;
  const chunks = _queuedChunks;
  _queuedChunks = null;
  await _transcribeAndSend(chunks);
}}

function _vadTick() {{
  const rms = _getRMS();
  const now = Date.now();
  if (rms > SILENCE_THRESHOLD) {{
    _silenceStartTime = null;
    if (!_isSpeechActive) {{
      _isSpeechActive = true;
      _speechStartTime = now;
      _micChunks = [];
      voiceOrb.classList.add("user-speaking");
      _startParticles("user");
    }}
  }} else if (_isSpeechActive) {{
    if (!_silenceStartTime) _silenceStartTime = now;
    const silenced  = now - _silenceStartTime;
    const speechDur = (_silenceStartTime || now) - (_speechStartTime || now);
    if (silenced >= SILENCE_SEND_MS && speechDur >= MIN_SPEECH_MS) {{
      _isSpeechActive = false;
      voiceOrb.classList.remove("user-speaking");
      _startParticles("idle");
      const chunks = [..._micChunks];
      _micChunks = [];
      _silenceStartTime = null;
      _speechStartTime  = null;
      if (_isTTSPlaying) {{
        _queuedChunks = chunks;
        statusEl.textContent = "Queued — waiting for playback to finish...";
      }} else {{
        _transcribeAndSend(chunks);
      }}
    }}
  }}
}}

async function _enableVoiceMode() {{
  _audioCtx    = new AudioContext({{ sampleRate: 16000 }});
  _mediaStream = await navigator.mediaDevices.getUserMedia({{ audio: true }});
  const source = _audioCtx.createMediaStreamSource(_mediaStream);
  _analyser    = _audioCtx.createAnalyser();
  _analyser.fftSize = 2048;
  _scriptProc  = _audioCtx.createScriptProcessor(4096, 1, 1);
  _micChunks   = [];
  _scriptProc.onaudioprocess = (e) => {{
    if (_isSpeechActive) _micChunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  }};
  source.connect(_analyser);
  source.connect(_scriptProc);
  _scriptProc.connect(_audioCtx.destination);
  _isSpeechActive = false; _speechStartTime = null; _silenceStartTime = null;
  _vadInterval = setInterval(_vadTick, 80);
  micBtn.classList.add("recording");
  micBtn.title = "Voice mode ON — click to disable";
  statusEl.textContent = "Listening...";
  _startParticles("idle");
}}

function _disableVoiceMode() {{
  if (_vadInterval)  {{ clearInterval(_vadInterval); _vadInterval = null; }}
  if (_scriptProc)   {{ _scriptProc.disconnect(); _scriptProc = null; }}
  if (_mediaStream)  {{ _mediaStream.getTracks().forEach(t => t.stop()); _mediaStream = null; }}
  if (_audioCtx)     {{ _audioCtx.close(); _audioCtx = null; }}
  _analyser = null; _micChunks = []; _isSpeechActive = false; _queuedChunks = null;
  micBtn.classList.remove("recording");
  micBtn.title = "Toggle voice input";
  statusEl.textContent = "";
  voiceOrb.classList.remove("user-speaking", "llm-speaking");
  _startParticles("idle");
}}

micBtn.addEventListener("click", async () => {{
  if (_voiceModeOn) {{
    _voiceModeOn = false;
    _disableVoiceMode();
  }} else {{
    try {{
      _voiceModeOn = true;
      await _enableVoiceMode();
    }} catch(e) {{
      _voiceModeOn = false;
      statusEl.textContent = "Mic error: " + e.message;
      setTimeout(() => statusEl.textContent = "", 3000);
    }}
  }}
}});

const TOOL_ICONS = {{
  list_dir:"📂", read_file:"📖", write_file:"✏️", run_external_process:"⚡",
  apply_patch:"🩹", propose_patch:"🔍", search_sessions:"🔎",
  scan_project_versions:"🔬", format_text:"📝", summarize_session:"📋",
  rename_session:"🏷️", burn_session:"🔥", get_last_version_scan:"📊",
}};

function addToolTimeline(toolLog) {{
  if (!toolLog || !toolLog.length) return;
  const timeline = document.createElement("div");
  timeline.className = "toolTimeline";
  for (const entry of toolLog) {{
    const card = document.createElement("div");
    card.className = "toolCard";
    const header = document.createElement("div");
    header.className = "toolCardHeader";
    const icon = TOOL_ICONS[entry.tool] || "🔧";
    header.innerHTML =
      `<span>${{icon}}</span>` +
      `<span class="toolName">${{entry.tool}}</span>` +
      `<span class="toolArgs">${{entry.args_preview || ""}}</span>` +
      `<span class="toolBadge ${{entry.status}}">${{entry.status}}</span>` +
      `<span class="toolCardChevron">▶</span>`;
    const body = document.createElement("div");
    body.className = "toolCardBody";
    body.textContent = entry.args_preview || "(no args)";
    header.addEventListener("click", () => card.classList.toggle("open"));
    card.appendChild(header);
    card.appendChild(body);
    timeline.appendChild(card);
  }}
  chatEl.appendChild(timeline);
  while (chatEl.children.length > UI_MAX_BUBBLES) chatEl.removeChild(chatEl.firstChild);
  scrollToBottomIfNeeded();
}}

async function apiGet(url) {{
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}}
async function apiPost(url, body) {{
  const r = await fetch(url, {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(body || {{}})
  }});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}}
async function apiDelete(url) {{
  const r = await fetch(url, {{ method: "DELETE" }});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}}

async function refreshSessions() {{
  const data = await apiGet("/api/sessions");
  const q = (searchEl.value || "").trim().toLowerCase();

  sessionsEl.innerHTML = "";
  const list = (data.sessions || []).filter(s => {{
    if (!q) return true;
    return (s.name || "").toLowerCase().includes(q) || (s.id || "").toLowerCase().includes(q);
  }});

  for (const s of list) {{
    const item = document.createElement("div");
    item.className = "sess" + (s.id === sessionId ? " active" : "");
    item.dataset.sid = s.id;

    const main = document.createElement("div");
    main.className = "sessMain";

    const t = document.createElement("div");
    t.className = "sessTitle";
    t.textContent = s.name || s.id;

    const meta = document.createElement("div");
    meta.className = "sessMeta";
    const left = document.createElement("span");
    left.textContent = (s.message_count ?? 0) + " msgs";
    const right = document.createElement("span");
    right.textContent = s.updated_ts ? fmtTime(s.updated_ts) : "";
    meta.appendChild(left);
    meta.appendChild(right);

    main.appendChild(t);
    main.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "sessActions";

    const burn = document.createElement("div");
    burn.className = "iconBtn";
    burn.title = "Burn session (permanently delete)";
    burn.innerHTML = '<span class="burnIcon">🔥</span>';

    burn.addEventListener("click", async (ev) => {{
      ev.stopPropagation();

      const name = s.name || s.id;
      const msg =
        "Permanently delete this session and ALL its messages?\\n\\n" +
        "Session: " + name + "\\n\\n" +
        "This cannot be undone. Type DELETE to confirm.";
      const confirmText = prompt(msg, "");
      if (confirmText !== "DELETE") return;

      const burningActive = (s.id === sessionId);
      if (burningActive) {{
        clearChat();
        sessionTitleEl.textContent = "(burned)";
      }}

      try {{
        await apiDelete(`/api/sessions/${{encodeURIComponent(s.id)}}`);
        await refreshSessions();

        if (burningActive) {{
          const ns = await apiPost("/api/sessions/new", {{}});
          await loadSession(ns.id);
        }}
      }} catch (e) {{
        alert("Burn failed: " + e.message);
        await refreshSessions();
        if (burningActive) {{
          await loadSession(sessionId);
        }}
      }}
    }});

    actions.appendChild(burn);
    item.appendChild(main);
    item.appendChild(actions);

    item.addEventListener("click", async () => {{
      await loadSession(s.id);
    }});

    item.addEventListener("contextmenu", async (ev) => {{
      ev.preventDefault();
      const current = s.name || s.id;
      const newName = prompt("Rename session:", current);
      if (newName === null) return;
      const nn = (newName || "").trim();
      if (!nn) return;
      try {{
        await apiPost(`/api/sessions/${{encodeURIComponent(s.id)}}/rename`, {{ name: nn }});
        await refreshSessions();
        if (s.id === sessionId) {{
          const loaded = await apiGet(`/api/sessions/${{encodeURIComponent(sessionId)}}`);
          applySessionHeader(loaded);
        }}
      }} catch (e) {{
        alert("Rename failed: " + e.message);
      }}
    }});

    sessionsEl.appendChild(item);
  }}
}}

function applySessionHeader(sessionObj) {{
  const nm = sessionObj?.name || sessionId;
  sessionTitleEl.textContent = "— " + nm;
  document.title = APP_NAME + " " + APP_VERSION + " — " + nm;
}}

async function loadSession(sid) {{
  sessionId = sid;
  localStorage.setItem("director_session_id", sessionId);

  statusEl.textContent = "Loading...";
  clearChat();

  const s = await apiGet(`/api/sessions/${{encodeURIComponent(sessionId)}}`);
  applySessionHeader(s);

  const msgs = s.messages || [];
  stickToBottom = true;

  for (const m of msgs) {{
    if (!m || !m.role) continue;
    if (m.role === "user" || m.role === "assistant") {{
      addBubble(m.role, m.content || "");
    }}
  }}

  scrollToBottomIfNeeded(true);
  statusEl.textContent = "";
  await refreshSessions();
}}

async function newSession() {{
  statusEl.textContent = "Creating session...";
  const s = await apiPost("/api/sessions/new", {{}});
  await loadSession(s.id);
  statusEl.textContent = "";
}}

async function apiChat(userText) {{
  const r = await fetch("/api/chat", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{
      session_id: sessionId,
      user_text: userText,
      auto_voice: getAutoVoice()
    }})
  }});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}}

async function apiVoice(text) {{
  const r = await fetch("/api/tts", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ text }})
  }});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}}

async function doSend() {{
  const text = msgEl.value.trim();
  if (!text) return;

  msgEl.value = "";
  addBubble("user", text);
  scrollToBottomIfNeeded(true);

  sendBtn.disabled = true;
  statusEl.textContent = "Thinking...";
  try {{
    const data = await apiChat(text);

    if (data.approval_required) {{
      addApprovalBubble(data.approval);
      _statusReset();
      await refreshSessions();
      return;
    }}

    addToolTimeline(data.tool_log);
    addBubble("assistant", data.reply || "");
    if (data.audio_url) {{
      statusEl.textContent = "Voicing...";
      playUrl(data.audio_url);
      // orb handled by player play/ended events
    }} else {{
      voiceOrb.classList.add("llm-speaking");
      _startParticles("llm");
      setTimeout(() => {{ voiceOrb.classList.remove("llm-speaking"); _startParticles("idle"); }}, 2000);
    }}
    _statusReset();
    await refreshSessions();
  }} catch (e) {{
    statusEl.textContent = "Error: " + e.message;
  }} finally {{
    sendBtn.disabled = false;
  }}
}}

sendBtn.addEventListener("click", doSend);

msgEl.addEventListener("keydown", (e) => {{
  if (e.key === "Enter" && !e.shiftKey) {{
    e.preventDefault();
    doSend();
  }}
}});

document.getElementById("voiceLast").addEventListener("click", async () => {{
  statusEl.textContent = "Voicing last...";
  try {{
    const data = await apiGet(`/api/last_assistant?session_id=${{encodeURIComponent(sessionId)}}`);
    if (!data.text) {{
      statusEl.textContent = "No assistant message yet.";
      return;
    }}
    const tts = await apiVoice(data.text);
    if (tts.audio_url) playUrl(tts.audio_url);
    statusEl.textContent = "";
  }} catch (e) {{
    statusEl.textContent = "Error: " + e.message;
  }}
}});

document.getElementById("newSessionBtn").addEventListener("click", async () => {{
  await newSession();
}});

searchEl.addEventListener("input", () => {{
  refreshSessions().catch(()=>{{}});
}});

(async () => {{
  try {{
    await refreshSessions();
    await loadSession(sessionId);
  }} catch (e) {{
    statusEl.textContent = "Init error: " + e.message;
  }}
}})();
</script>
</body>
</html>
"""

# ----------------------------
# End of file
# ----------------------------