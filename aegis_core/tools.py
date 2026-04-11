from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Callable

# ----------------------------
# Tool Registry helpers (extracted)
# ----------------------------
ToolLevel = str  # "safe" | "risky"

@dataclass
class ToolDef:
    name: str
    level: ToolLevel
    description: str
    schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Dict[str, Any]]

def tool_level(tools: Dict[str, ToolDef], name: str) -> ToolLevel:
    td = tools.get(name)
    return td.level if td else "risky"

def requires_approval(aegis_mode: str, tools: Dict[str, ToolDef], tool_name: str) -> bool:
    if str(aegis_mode).strip().lower() != "semi":
        return False
    return tool_level(tools, tool_name) == "risky"

def execute_tool(tools: Dict[str, ToolDef], tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    td = tools.get(tool_name)
    if not td:
        return {"ok": False, "error": f"Unknown tool: {tool_name}"}
    try:
        return td.handler(args)
    except Exception as e:
        return {"ok": False, "error": str(e)}

def build_system_tool_instructions(app_name: str, allowed_roots: Any, tools: Dict[str, ToolDef]) -> str:
    # Keep content identical to the inline app.py string; only moved.
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
