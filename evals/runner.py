"""
Aegis AI — Eval Harness
Runs benchmark tasks against the tool registry and scores results.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any, Dict, List

TASKS_PATH = Path(__file__).parent / "tasks.json"
RESULTS_PATH = Path(__file__).parent / "results.json"

def run_evals(tools: Dict, sessions_dir: str) -> Dict[str, Any]:
    tasks = json.loads(TASKS_PATH.read_text())
    results = []
    passed = 0

    for task in tasks:
        tool_def = tools.get(task["tool"])
        if not tool_def:
            results.append({"id": task["id"], "status": "skip", "reason": "tool not found", "duration_ms": 0})
            continue

        start = time.perf_counter()
        try:
            # Inject sessions_dir for tools that need it
            args = dict(task["args"])
            if task["tool"] in ("search_sessions", "summarize_session"):
                args.setdefault("_sessions_dir", sessions_dir)

            result = tool_def.handler(args)
            duration_ms = round((time.perf_counter() - start) * 1000)
            ok = task["expect_key"] in result or result.get("ok") or result.get("items") is not None
            status = "pass" if ok else "fail"
            if ok:
                passed += 1
            results.append({
                "id": task["id"],
                "description": task["description"],
                "status": status,
                "duration_ms": duration_ms,
            })
        except Exception as e:
            duration_ms = round((time.perf_counter() - start) * 1000)
            results.append({
                "id": task["id"],
                "description": task["description"],
                "status": "error",
                "reason": str(e),
                "duration_ms": duration_ms,
            })

    summary = {
        "passed": passed,
        "total": len(tasks),
        "score": f"{passed}/{len(tasks)}",
        "results": results,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    RESULTS_PATH.write_text(json.dumps(summary, indent=2))
    return summary
