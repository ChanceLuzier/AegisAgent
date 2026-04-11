from __future__ import annotations

from typing import Any, Callable, Dict, List

from aegis_core.tools import ToolDef


def build_tools_registry(
    *,
    tool_summarize_session: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_search_sessions: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_rename_session: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_format_text: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_scan_project_versions: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_get_last_version_scan: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_propose_patch: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_burn_session: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_list_dir: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_read_file: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_write_file: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_run_external_process: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_apply_patch: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_stub: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, ToolDef]:
    """Build the ToolDef registry without changing behavior.

    Tool implementations remain defined in app.py (they can close over app-level helpers/state).
    """
    return {
        # SAFE
        "summarize_session": ToolDef(
            "summarize_session",
            "safe",
            "Summarize the last messages in a session.",
            {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
            tool_summarize_session,
        ),
        "search_sessions": ToolDef(
            "search_sessions",
            "safe",
            "Search sessions by name/id and message contents.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            tool_search_sessions,
        ),
        "rename_session": ToolDef(
            "rename_session",
            "safe",
            "Rename a session.",
            {"type": "object", "properties": {"session_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["session_id", "name"]},
            tool_rename_session,
        ),
        "format_text": ToolDef(
            "format_text",
            "safe",
            "Format text (clean/bullets).",
            {"type": "object", "properties": {"text": {"type": "string"}, "style": {"type": "string"}}, "required": ["text"]},
            tool_format_text,
        ),
        "scan_project_versions": ToolDef(
            "scan_project_versions",
            "safe",
            "Grounded scan for version strings in real project files (no hallucinated files).",
            {"type": "object", "properties": {"pattern": {"type": "string"}, "exts": {"type": "array", "items": {"type": "string"}}, "max_files": {"type": "integer"}, "max_matches_per_file": {"type": "integer"}}, "required": []},
            tool_scan_project_versions,
        ),
        "get_last_version_scan": ToolDef(
            "get_last_version_scan",
            "safe",
            "Return the last grounded version scan results.",
            {"type": "object", "properties": {}, "required": []},
            tool_get_last_version_scan,
        ),
        # Patch workflow
        "propose_patch": ToolDef(
            "propose_patch",
            "safe",
            "Validate a unified diff and request approval to apply it.",
            {"type": "object", "properties": {"path": {"type": "string"}, "diff": {"type": "string"}}, "required": ["path", "diff"]},
            tool_propose_patch,
        ),
        # RISKY
        "burn_session": ToolDef(
            "burn_session",
            "risky",
            "Permanently delete a session file.",
            {"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]},
            tool_burn_session,
        ),
        # NOTE: keep level string identical to current app behavior.
        "list_dir": ToolDef(
            "list_dir",
            "safe",
            "List files/folders in a directory (restricted to allowed roots).",
            {"type": "object", "properties": {"path": {"type": "string"}, "include_hidden": {"type": "boolean"}, "max_items": {"type": "integer"}}, "required": ["path"]},
            tool_list_dir,
        ),
        "read_file": ToolDef(
            "read_file",
            "risky",
            "Read a text file (restricted to allowed roots, size-capped).",
            {"type": "object", "properties": {"path": {"type": "string"}, "encoding": {"type": "string"}}, "required": ["path"]},
            tool_read_file,
        ),
        "write_file": ToolDef(
            "write_file",
            "risky",
            "Write/overwrite a text file (restricted to allowed roots, size-capped).",
            {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "encoding": {"type": "string"}, "mkdirs": {"type": "boolean"}}, "required": ["path", "content"]},
            tool_write_file,
        ),
        "run_external_process": ToolDef(
            "run_external_process",
            "risky",
            "Run an external command (capture output, timeout).",
            {"type": "object", "properties": {"cmd": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string"}, "timeout_sec": {"type": "integer"}, "env": {"type": "object"}}, "required": ["cmd"]},
            tool_run_external_process,
        ),
        # Patch apply (risky)
        "apply_patch": ToolDef(
            "apply_patch",
            "risky",
            "Apply a validated unified diff to a file (robust, context-checked).",
            {"type": "object", "properties": {"path": {"type": "string"}, "diff": {"type": "string"}}, "required": ["path", "diff"]},
            tool_apply_patch,
        ),
        # Stubs
        "execute_ffmpeg": ToolDef(
            "execute_ffmpeg",
            "risky",
            "Execute ffmpeg (stub).",
            {"type": "object", "properties": {"args": {"type": "array", "items": {"type": "string"}}, "required": ["args"]}},
            tool_stub,
        ),
        "queue_comfyui_job": ToolDef(
            "queue_comfyui_job",
            "risky",
            "Queue ComfyUI (stub).",
            {"type": "object", "properties": {"workflow": {"type": "object"}}, "required": ["workflow"]},
            tool_stub,
        ),
    }


def build_system_tool_instructions(app_name: str, allowed_roots: List[str], tools: Dict[str, ToolDef]) -> str:
    """Keep the exact SYSTEM_TOOL_INSTRUCTIONS string previously built inline in app.py."""
    return (
        f"You are {app_name}, a local privacy-first assistant.\n"
        "Tools are OPTIONAL, but if the user asks about local files/folders, prefer using filesystem tools.\n"
        "When you want to use a tool, output ONLY a single JSON object (no prose, no code fences),\n"
        "with the shape:\n"
        '{ "tool": "<tool_name>", "args": { ... }, "reason": "<short reason>" }\n\n'
        "If you do NOT need a tool, respond normally.\n\n"
        "Available tools:\n"
        + "\n".join([f"- {name} ({td.level}): {td.description}" for name, td in tools.items()])
        + "\n\nRules:\n"
        "- Never invent tools.\n"
        "- SAFE tools can be executed automatically.\n"
        "- RISKY tools require user approval in semi mode.\n"
        f"- Filesystem tools are restricted to these allowed roots: {allowed_roots}\n"
        "- For patching code: use propose_patch (SAFE) with a unified diff, then wait for approval to apply.\n"
        "After a tool result, you MUST answer the user's question with details.\n"
        "Never reply only with 'Done.'\n"
    )
