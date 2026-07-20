from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any

from server.common.paths import NERON_WORKSPACE_DIR
from goal.tools.code_generator import (
    DeterministicToolCodeGenerator,
    OllamaToolCodeGenerator,
    ToolCodeGenerator,
    validate_generated_code,
    validate_workspace_path,
)
from goal.infra.events import Event, event_bus, TOOL_CREATED
from goal.tools.models import ToolNeed, ToolResult, ToolSpec
from goal.tools.registry import ToolRegistry, get_tool_registry
from goal.tools.runtime import ToolRuntime, get_tool_runtime
from goal.tools.spec_builder import ToolSpecBuilder


LOG_TOOL_SLUGS = (
    "neron_log_reader_tool",
    "neron_log_error_filter_tool",
    "neron_log_summary_tool",
)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    return " ".join(normalized.split())


class ToolCreator:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        runtime: ToolRuntime | None = None,
        *,
        workspace: Path = NERON_WORKSPACE_DIR / "tools",
        generated_tools: Path | None = None,
        spec_builder: ToolSpecBuilder | None = None,
        deterministic_generator: ToolCodeGenerator | None = None,
        ollama_generator: ToolCodeGenerator | None = None,
    ) -> None:
        self.registry = registry or get_tool_registry()
        self.runtime = runtime or (
            ToolRuntime(self.registry) if registry is not None else get_tool_runtime()
        )
        self.workspace = workspace
        self.generated_tools = generated_tools or (
            workspace.parent.parent / "server" / "tools" / "generated"
        )
        self.spec_builder = spec_builder or ToolSpecBuilder()
        self.deterministic_generator = (
            deterministic_generator or DeterministicToolCodeGenerator()
        )
        self.ollama_generator = ollama_generator or OllamaToolCodeGenerator(
            workspace=workspace
        )

    def plan_tools_for_request(self, text: str) -> list[ToolSpec]:
        query = _normalize(text)
        if "log" not in query and "journal" not in query:
            return []
        return self._log_tool_specs()

    def create_tool(self, spec: ToolSpec) -> ToolSpec:
        errors = self.validate_tool(spec)
        if errors:
            raise ValueError("; ".join(errors))
        return self.register_tool(spec)

    def validate_tool(self, spec: ToolSpec) -> list[str]:
        errors: list[str] = []
        if not spec.name.strip():
            errors.append("tool_name_required")
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", spec.slug):
            errors.append("invalid_tool_slug")
        if not spec.description.strip():
            errors.append("tool_description_required")
        if spec.safety.get("allow_system_commands"):
            errors.append("system_commands_not_allowed")
        return errors

    def register_tool(self, spec: ToolSpec) -> ToolSpec:
        return self.registry.register_tool(spec)

    def ensure_tools_for_request(self, text: str) -> dict[str, Any]:
        planned = self.plan_tools_for_request(text)
        created: list[str] = []
        existing: list[str] = []
        for spec in planned:
            if self.registry.tool_exists(spec.slug):
                existing.append(spec.slug)
                continue
            self.create_tool(spec)
            created.append(spec.slug)
        required = [spec.slug for spec in planned]
        return {
            "status": "ready" if required else "not_planned",
            "required_tools": required,
            "created_tools": created,
            "existing_tools": existing,
            "tool_creation_status": "ready" if required else "not_required",
        }

    def plan_need(
        self,
        text: str,
        *,
        domain: str | None = None,
        intent: str | None = None,
        required_tool_slugs: list[str] | None = None,
        matched_capability: str | None = None,
    ) -> ToolNeed:
        normalized = _normalize(text)
        resolved_domain = domain or self._infer_domain(normalized)
        resolved_intent = self._canonical_intent(
            intent or self._infer_intent(normalized),
            normalized,
        )
        metadata: dict[str, Any] = {}
        if required_tool_slugs:
            metadata["required_tool_slugs"] = list(required_tool_slugs)
        if matched_capability:
            metadata["matched_capability"] = matched_capability
        return ToolNeed(
            request_text=text,
            domain=resolved_domain,
            intent=resolved_intent,
            capability_goal=f"{resolved_intent} le domaine {resolved_domain}",
            expected_outputs=["status", "summary", "issues", "recommendation"],
            safety_level="low",
            metadata=metadata,
        )

    def plan_from_need(self, need: ToolNeed) -> dict[str, Any]:
        specs = self.spec_builder.build_specs_from_need(need)
        deterministic = self.deterministic_generator.can_generate(need)
        return {
            "need": need.to_dict(),
            "required_tools": [spec.slug for spec in specs],
            "specs": [spec.to_dict() for spec in specs],
            "creation_strategy": (
                "deterministic" if deterministic else "ollama_required"
            ),
        }

    async def create_from_need(self, need: ToolNeed) -> dict[str, Any]:
        plan = self.plan_from_need(need)
        specs = self.spec_builder.build_specs_from_need(need)
        created: list[str] = []
        reused: list[str] = []
        errors: list[str] = []
        strategies: set[str] = set()
        self.workspace.mkdir(parents=True, exist_ok=True)

        for spec in specs:
            if self.registry.tool_exists(spec.slug):
                reused.append(spec.slug)
                continue
            try:
                tool_path = self.workspace / f"{spec.slug}.py"
                test_path = self.workspace / "tests" / f"test_{spec.slug}.py"
                need.metadata["tool_path"] = str(tool_path)
                strategy, code, test_code = await self._generate_artifacts(spec, need)
                strategies.add(strategy)
                code_errors = validate_generated_code(code)
                if code_errors:
                    raise ValueError(",".join(code_errors))
                if not validate_workspace_path(tool_path, self.workspace):
                    raise ValueError("tool_path_outside_workspace")
                if not validate_workspace_path(test_path, self.workspace):
                    raise ValueError("test_path_outside_workspace")
                test_path.parent.mkdir(parents=True, exist_ok=True)
                tool_path.write_text(code, encoding="utf-8")
                test_path.write_text(test_code, encoding="utf-8")
                await event_bus.publish(Event(
                    type=TOOL_CREATED,
                    source="tool_creator",
                    payload={
                        "tool_slug": spec.slug,
                        "path": str(tool_path),
                        "test_path": str(test_path),
                        "status": "draft",
                    },
                ))
                self._validate_generated_tool(test_path)
                self.generated_tools.mkdir(parents=True, exist_ok=True)
                promoted_path = self.generated_tools / tool_path.name
                shutil.copy2(tool_path, promoted_path)
                handler = self._load_handler(promoted_path)
                spec.metadata["workspace_tool_path"] = str(tool_path)
                spec.metadata["tool_path"] = str(promoted_path)
                spec.metadata["handler_path"] = str(promoted_path)
                spec.metadata["test_path"] = str(test_path)
                spec.metadata["validated_generated_tool"] = True
                spec.source = (
                    "tool_creator_deterministic"
                    if strategy == "deterministic"
                    else "tool_creator_ollama"
                )
                self.runtime.register_handler(spec.slug, handler)
                self.create_tool(spec)
                created.append(spec.slug)
            except Exception as exc:
                errors.append(f"{spec.slug}:{exc}")

        if errors:
            return {
                **plan,
                "status": "failed",
                "created_tools": created,
                "reused_tools": reused,
                "errors": errors,
            }
        strategy = (
            "ollama_required"
            if "ollama_required" in strategies
            else plan["creation_strategy"]
        )
        return {
            **plan,
            "status": "completed",
            "creation_strategy": strategy,
            "created_tools": created,
            "reused_tools": reused,
            "errors": [],
        }

    async def execute_tools_for_request(
        self,
        text: str,
        payload: dict[str, Any] | None = None,
    ) -> ToolResult:
        ensured = self.ensure_tools_for_request(text)
        if ensured["required_tools"] != list(LOG_TOOL_SLUGS):
            return ToolResult(ok=False, error="tool_plan_not_available")
        reader = await self.runtime.execute_tool(
            "neron_log_reader_tool",
            payload or {},
        )
        if not reader.ok:
            return reader
        filtered = await self.runtime.execute_tool(
            "neron_log_error_filter_tool",
            {"logs": reader.data.get("logs", [])},
        )
        if not filtered.ok:
            return filtered
        return await self.runtime.execute_tool(
            "neron_log_summary_tool",
            {"errors": filtered.data.get("errors", [])},
        )

    async def _generate_artifacts(
        self,
        spec: ToolSpec,
        need: ToolNeed,
    ) -> tuple[str, str, str]:
        if self.deterministic_generator.can_generate(need):
            try:
                code = await self.deterministic_generator.generate_tool_code(
                    spec, need
                )
                test_code = await self.deterministic_generator.generate_tests(
                    spec, need
                )
                return "deterministic", code, test_code
            except Exception:
                pass
        if not self.ollama_generator.can_generate(need):
            raise RuntimeError("tool_generation_unavailable")
        code = await self.ollama_generator.generate_tool_code(spec, need)
        test_code = await self.ollama_generator.generate_tests(spec, need)
        return "ollama_required", code, test_code

    @staticmethod
    def _load_handler(path: Path):
        module_spec = importlib.util.spec_from_file_location(
            f"generated_tool_{path.stem}",
            path,
        )
        if not module_spec or not module_spec.loader:
            raise RuntimeError("generated_tool_import_failed")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        handler = getattr(module, "execute", None)
        if not callable(handler):
            raise RuntimeError("generated_tool_execute_missing")
        return handler

    @staticmethod
    def _validate_generated_tool(test_path: Path) -> None:
        env = os.environ.copy()
        server_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{server_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else server_root
        )
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(test_path)],
            text=True,
            capture_output=True,
            timeout=120,
            env=env,
        )
        if completed.returncode != 0:
            error = completed.stdout[-2000:] or completed.stderr[-2000:]
            raise RuntimeError(f"tool_tests_failed:{error}")

    @staticmethod
    def _infer_domain(normalized: str) -> str:
        domains = (
            (("sauvegarde", "backup"), "backups"),
            (("sqlite", "base de donnees", "database"), "sqlite"),
            (("systemd", "service"), "systemd"),
            (("log", "journal"), "neron_logs"),
        )
        for aliases, domain in domains:
            if any(alias in normalized for alias in aliases):
                return domain
        return "generic"

    @staticmethod
    def _infer_intent(normalized: str) -> str:
        if any(token in normalized for token in ("combien", "compte", "nombre")):
            return "count"
        if any(token in normalized for token in ("diagnostic", "diagnostique")):
            return "diagnose"
        if any(token in normalized for token in ("surveille", "monitor")):
            return "monitor"
        if any(token in normalized for token in ("resume", "synthese")):
            return "summarize"
        if any(token in normalized for token in ("calcule", "calcul")):
            return "calculate"
        return "analyze"

    @staticmethod
    def _canonical_intent(intent: str, normalized: str) -> str:
        value = _normalize(intent)
        aliases = {
            "analyse": "analyze",
            "analyser": "analyze",
            "resume": "summarize",
            "resumer": "summarize",
            "diagnostic": "diagnose",
            "diagnostiquer": "diagnose",
            "surveillance": "monitor",
            "surveiller": "monitor",
            "notification": "notify",
            "notifier": "notify",
            "calcul": "calculate",
            "calculer": "calculate",
            "recherche": "search",
            "rechercher": "search",
            "execution": "execute",
            "executer": "execute",
        }
        if value == "calcul" and any(
            token in normalized for token in ("combien", "compte", "nombre")
        ):
            return "count"
        return aliases.get(value, value)

    def _log_tool_specs(self) -> list[ToolSpec]:
        common_safety = {
            "level": "low",
            "allow_system_commands": False,
            "network_access": False,
            "filesystem_access": False,
        }
        return [
            ToolSpec(
                name="Lecteur de logs Néron",
                slug="neron_log_reader_tool",
                description=(
                    "Collecte des lignes de logs fournies par un payload ou un provider injecté."
                ),
                inputs={"logs": "string|list[string]", "limit": "integer"},
                outputs={"logs": "list[string]", "line_count": "integer"},
                safety=dict(common_safety),
                metadata={"aliases": ["lire les logs Néron", "logs Néron"]},
            ),
            ToolSpec(
                name="Filtre d'erreurs des logs Néron",
                slug="neron_log_error_filter_tool",
                description=(
                    "Extrait les lignes ERROR, CRITICAL, FATAL, Traceback et Exception."
                ),
                inputs={"logs": "list[string]"},
                outputs={"errors": "list[string]", "error_count": "integer"},
                safety=dict(common_safety),
                metadata={"aliases": ["filtrer les erreurs des logs"]},
            ),
            ToolSpec(
                name="Résumé des erreurs des logs Néron",
                slug="neron_log_summary_tool",
                description=(
                    "Résume le nombre d'erreurs, les composants, la gravité et une recommandation."
                ),
                inputs={"errors": "list[string]"},
                outputs={
                    "error_count": "integer",
                    "components": "list[string]",
                    "severity": "string",
                    "excerpt": "string",
                    "recommendation": "string",
                },
                safety=dict(common_safety),
                metadata={"aliases": ["résumer les erreurs critiques"]},
            ),
        ]


_CREATOR: ToolCreator | None = None
_CREATOR_LOCK = threading.Lock()


def get_tool_creator() -> ToolCreator:
    global _CREATOR
    with _CREATOR_LOCK:
        if _CREATOR is None:
            _CREATOR = ToolCreator()
        return _CREATOR
