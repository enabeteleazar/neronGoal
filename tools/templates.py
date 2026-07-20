from __future__ import annotations

import json

from goal.tools.models import ToolNeed, ToolSpec


ISSUE_KEYWORDS = (
    "error",
    "failed",
    "failure",
    "corrupted",
    "corrupt",
    "invalid",
    "missing",
    "timeout",
    "critical",
)


def deterministic_tool_code(spec: ToolSpec, need: ToolNeed) -> str:
    role = str(spec.metadata.get("role") or "processor")
    return f'''from __future__ import annotations

from goal.tools.models import ToolResult


ROLE = {role!r}
DOMAIN = {need.domain!r}
ISSUE_KEYWORDS = {ISSUE_KEYWORDS!r}


def _entries(payload):
    value = payload.get("items")
    if value is None:
        value = payload.get("entries")
    if value is None:
        value = payload.get("data")
    if value is None:
        value = payload.get("text")
    if value is None:
        value = []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, dict):
        return [{{"key": key, "value": item}} for key, item in value.items()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def execute(payload):
    entries = _entries(dict(payload or {{}}))
    if ROLE in {{"reader", "collector", "status"}}:
        return ToolResult(
            ok=True,
            response=f"{{len(entries)}} entrée(s) {{DOMAIN}} normalisée(s).",
            data={{"entries": entries, "count": len(entries), "status": "ok"}},
        )

    if ROLE in {{"analyzer", "diagnosis", "detector"}}:
        issues = [
            item for item in entries
            if any(keyword in str(item).lower() for keyword in ISSUE_KEYWORDS)
        ]
        status = "issues_detected" if issues else "ok"
        return ToolResult(
            ok=True,
            response=f"Analyse {{DOMAIN}} : {{len(issues)}} problème(s) détecté(s).",
            data={{"issues": issues, "issue_count": len(issues), "status": status}},
        )

    if ROLE == "summary":
        issues = payload.get("issues") or entries
        if isinstance(issues, str):
            issues = [issues]
        issues = list(issues)
        status = "issues_detected" if issues else "ok"
        summary = (
            f"{{len(issues)}} problème(s) détecté(s) pour {{DOMAIN}}."
            if issues else f"Aucun problème détecté pour {{DOMAIN}}."
        )
        recommendation = (
            "Vérifier les éléments signalés et leur source."
            if issues else "Aucune action immédiate."
        )
        return ToolResult(
            ok=True,
            response=summary,
            data={{
                "status": status,
                "summary": summary,
                "issues": issues,
                "recommendation": recommendation,
            }},
        )

    if ROLE == "counter":
        return ToolResult(
            ok=True,
            response=f"{{len(entries)}} élément(s) {{DOMAIN}}.",
            data={{"count": len(entries), "status": "ok"}},
        )

    if ROLE == "comparator":
        reference = payload.get("reference")
        differences = [item for item in entries if item != reference]
        return ToolResult(
            ok=True,
            response=f"{{len(differences)}} différence(s) détectée(s).",
            data={{"differences": differences, "count": len(differences), "status": "ok"}},
        )

    if ROLE == "search":
        query = str(payload.get("query") or "").lower()
        matches = [item for item in entries if query in str(item).lower()]
        return ToolResult(
            ok=True,
            response=f"{{len(matches)}} correspondance(s) trouvée(s).",
            data={{"matches": matches, "count": len(matches), "status": "ok"}},
        )

    if ROLE == "calculator":
        values = payload.get("values") or entries
        numeric = [value for value in values if isinstance(value, (int, float))]
        value = sum(numeric) if numeric else payload.get("value")
        return ToolResult(
            ok=True,
            response=f"Résultat calculé : {{value}}.",
            data={{"value": value, "status": "ok"}},
        )

    return ToolResult(
        ok=True,
        response=f"Payload {{DOMAIN}} traité.",
        data={{"status": "ok", "result": entries}},
    )
'''


def deterministic_tool_test(spec: ToolSpec, tool_path: str) -> str:
    return f'''from __future__ import annotations

import importlib.util

from goal.tools.models import ToolResult


def test_generated_tool_returns_tool_result():
    spec = importlib.util.spec_from_file_location("generated_tool", {tool_path!r})
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    result = module.execute({{"items": ["ok", "ERROR failed"]}})
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.response or result.data
'''


def codex_prompt(spec: ToolSpec, need: ToolNeed, *, artifact: str) -> str:
    return "\n".join(
        [
            f"Generate only the Python {artifact} content for tool {spec.slug}.",
            f"Need: {json.dumps(need.to_dict(), ensure_ascii=False, sort_keys=True)}",
            f"Spec: {json.dumps(spec.to_dict(), ensure_ascii=False, sort_keys=True)}",
            "The tool must be deterministic and payload-only.",
            "No os, subprocess, socket, shutil, pathlib, network, filesystem, eval, or exec.",
            "The implementation must expose execute(payload) returning ToolResult.",
        ]
    )
