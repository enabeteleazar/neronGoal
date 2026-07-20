from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from goal.infra.events import Event
from goal.infra.events import event_bus
from goal.infra.events import (
    TOOL_EXECUTION_COMPLETED,
    TOOL_EXECUTION_STARTED,
)
from goal.tools.models import ToolResult
from goal.tools.registry import ToolRegistry, get_tool_registry

logger = logging.getLogger("goal.tools.runtime")

ToolHandler = Callable[[dict[str, Any]], ToolResult | dict[str, Any] | Awaitable[Any]]
_ERROR_PATTERN = re.compile(
    r"\b(ERROR|CRITICAL|FATAL|Traceback|Exception)\b",
    re.IGNORECASE,
)
_COMPONENT_PATTERNS = (
    re.compile(r"\b(?:component|service|module)[=: ]+([A-Za-z0-9_.-]+)", re.IGNORECASE),
    re.compile(r"\[([A-Za-z0-9_.-]+)\]"),
    re.compile(
        r"^\s*(?:ERROR|CRITICAL|FATAL|Traceback|Exception)"
        r"(?:\s*[:|-]\s*|\s+)([A-Za-z][A-Za-z0-9_.-]*)\b",
        re.IGNORECASE,
    ),
)


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        handlers: dict[str, ToolHandler] | None = None,
        log_provider: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.registry = registry or get_tool_registry()
        self.log_provider = log_provider

        self.handlers: dict[str, ToolHandler] = {
            "neron_log_reader_tool": self._read_logs,
            "neron_log_error_filter_tool": self._filter_errors,
            "neron_log_summary_tool": self._summarize_errors,
            **(handlers or {}),
        }
        # integrations.homeassistant.tool : dépendance monolithe non encore
        # auditée/rapatriée (hors du périmètre de la phase 6, qui porte sur
        # la génération de code). Enregistrement optionnel et tolérant :
        # si absente, le runtime démarre quand même sans ce handler intégré.
        try:
            from integrations.homeassistant.tool import (
                HOMEASSISTANT_TOOL_SLUG,
                execute as execute_homeassistant,
            )
            self.handlers[HOMEASSISTANT_TOOL_SLUG] = execute_homeassistant
        except ModuleNotFoundError:
            logger.warning(
                "integrations.homeassistant.tool indisponible sur cette machine "
                "— outil home assistant non enregistré (à traiter dans une phase dédiée)."
            )
        self._ensure_builtin_tools()

    def register_handler(self, slug: str, handler: ToolHandler) -> None:
        self.handlers[slug] = handler

    def _ensure_builtin_tools(self) -> None:
        try:
            from integrations.homeassistant.tool import (
                HOMEASSISTANT_TOOL_SLUG,
                tool_spec as homeassistant_tool_spec,
            )
        except ModuleNotFoundError:
            return
        if self.registry.get_tool(HOMEASSISTANT_TOOL_SLUG) is None:
            self.registry.register_tool(homeassistant_tool_spec())

    async def execute_tool(
        self,
        slug: str,
        payload: dict[str, Any] | None = None,
    ) -> ToolResult:
        spec = self.registry.get_tool(slug)
        if spec is None:
            return ToolResult(ok=False, error="tool_not_found")
        if spec.safety.get("allow_system_commands"):
            return ToolResult(ok=False, error="unsafe_tool_rejected")
        declared_path = self._registered_handler_path(spec)
        if declared_path is not None:
            if "workspace" in declared_path.parts:
                return ToolResult(ok=False, error="tool_workspace_handler_rejected")
            if not declared_path.is_file():
                return ToolResult(ok=False, error="tool_handler_not_available")
        handler = self.handlers.get(slug)
        if handler is None:
            try:
                handler = self._load_registered_handler(spec)
            except Exception:
                return ToolResult(ok=False, error="tool_handler_not_available")
            if handler is not None:
                self.handlers[slug] = handler
        if handler is None:
            return ToolResult(ok=False, error="tool_handler_not_available")
        event_bus.publish_background(Event(
            type=TOOL_EXECUTION_STARTED,
            source="tool_runtime",
            payload={"tool_slug": slug},
        ))
        try:
            value = handler(dict(payload or {}))
            if inspect.isawaitable(value):
                value = await value
            result = self._coerce_result(value)
        except Exception as exc:
            result = ToolResult(ok=False, error=str(exc))
        event_bus.publish_background(Event(
            type=TOOL_EXECUTION_COMPLETED,
            source="tool_runtime",
            payload={
                "tool_slug": slug,
                "status": "completed" if result.ok else "failed",
                "error": result.error,
            },
        ))
        return result

    @staticmethod
    def _registered_handler_path(spec) -> Path | None:
        path = spec.metadata.get("handler_path") or spec.metadata.get("tool_path")
        return Path(str(path)) if path else None

    @classmethod
    def _load_registered_handler(cls, spec):
        path = cls._registered_handler_path(spec)
        if path is None or not path.is_file():
            return None
        module_spec = importlib.util.spec_from_file_location(
            f"registered_tool_{spec.slug}",
            path,
        )
        if not module_spec or not module_spec.loader:
            return None
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        handler = getattr(module, "execute", None)
        return handler if callable(handler) else None

    def _read_logs(self, payload: dict[str, Any]) -> ToolResult:
        logs = payload.get("logs")
        if logs is None and self.log_provider is not None:
            logs = self.log_provider(payload)
        if logs is None:
            logs = []
        if isinstance(logs, str):
            lines = logs.splitlines()
        else:
            lines = [str(line) for line in logs]
        limit = max(1, min(int(payload.get("limit", 500)), 2000))
        selected = lines[-limit:]
        return ToolResult(
            ok=True,
            response=f"{len(selected)} lignes de log disponibles.",
            data={"logs": selected, "line_count": len(selected)},
        )

    def _filter_errors(self, payload: dict[str, Any]) -> ToolResult:
        logs = payload.get("logs") or []
        if isinstance(logs, str):
            logs = logs.splitlines()
        errors = [str(line) for line in logs if _ERROR_PATTERN.search(str(line))]
        return ToolResult(
            ok=True,
            response=f"{len(errors)} erreurs extraites des logs.",
            data={"errors": errors, "error_count": len(errors)},
        )

    def _summarize_errors(self, payload: dict[str, Any]) -> ToolResult:
        effective_payload = payload
        nested_payload = payload.get("payload")
        if isinstance(nested_payload, dict):
            effective_payload = {**nested_payload, **payload}
        errors = effective_payload.get("errors") or []
        if isinstance(errors, str):
            errors = errors.splitlines()
        lines = [str(line).strip() for line in errors if str(line).strip()]
        if not lines:
            return ToolResult(
                ok=True,
                response="Analyse des logs Néron : aucune erreur critique détectée.",
                data={
                    "error_count": 0,
                    "components": [],
                    "severity": "none",
                    "excerpt": "",
                    "recommendation": "Aucune action immédiate.",
                },
            )

        severity_counts = Counter(
            match.group(1).upper()
            for line in lines
            for match in [_ERROR_PATTERN.search(line)]
            if match
        )
        severity = next(
            (level for level in ("FATAL", "CRITICAL", "EXCEPTION", "TRACEBACK", "ERROR")
             if severity_counts[level]),
            "ERROR",
        ).lower()
        components = sorted(
            {
                match.group(1)
                for line in lines
                for pattern in _COMPONENT_PATTERNS
                for match in [pattern.search(line)]
                if match
            }
        )
        excerpt = lines[0][:180]
        component_text = ", ".join(components[:5]) if components else "non identifié"
        response = (
            f"Analyse des logs Néron : {len(lines)} erreur(s), gravité {severity}. "
            f"Composants : {component_text}. Extrait : {excerpt}. "
            "Recommandation : vérifier le composant concerné et corréler les événements voisins."
        )
        return ToolResult(
            ok=True,
            response=response,
            data={
                "error_count": len(lines),
                "components": components,
                "severity": severity,
                "excerpt": excerpt,
                "recommendation": (
                    "Vérifier le composant concerné et corréler les événements voisins."
                ),
            },
        )

    def _coerce_result(self, value: Any) -> ToolResult:
        if isinstance(value, ToolResult):
            return value
        if isinstance(value, dict):
            if "ok" in value:
                return ToolResult(
                    ok=bool(value.get("ok")),
                    response=str(value.get("response") or ""),
                    data=dict(value.get("data") or {}),
                    error=str(value["error"]) if value.get("error") else None,
                )
            return ToolResult(ok=True, data=value)
        return ToolResult(ok=True, response=str(value or ""))


_RUNTIME: ToolRuntime | None = None
_RUNTIME_LOCK = threading.Lock()


def get_tool_runtime() -> ToolRuntime:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            _RUNTIME = ToolRuntime()
        return _RUNTIME
