from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from server.common.paths import NERON_ROOT
from typing import Any, Literal

from goal.agents_factory.registry import DEFAULT_GENERATED_AGENTS, DynamicAgentRegistry
from goal.agents_factory.promotion import AgentPromotionService
from goal.agents_factory.validator import validate_agent
from goal.agents_factory.code_generator import OllamaAgentCodeGenerator
from goal.infra.agent_sandbox import AgentSandbox
from goal.infra.business_validator import BusinessValidator
from goal.infra.events import Event, event_bus, AGENT_CREATED, AGENT_PROMOTED, AGENT_REGISTERED
from goal.infra.critic_engine import SENSITIVE_KEYWORDS, normalize_for_keyword_match
from goal.goals.execution_engine import GoalExecutionEngine
from goal.projects.manager import ProjectManager, get_project_manager


DEFAULT_PROJECT_ROOT = Path(os.getenv("NERON_PROJECT_ROOT", str(NERON_ROOT)))
DEFAULT_WORKSPACE_ROOT = Path(
    os.getenv("NERON_WORKSPACE", str(DEFAULT_PROJECT_ROOT / "workspace"))
)
DEFAULT_WORKSPACE_AGENTS = DEFAULT_WORKSPACE_ROOT / "agents"
DEFAULT_WORKSPACE_TESTS = DEFAULT_WORKSPACE_ROOT / "agent_tests"
# NOTE : la valeur "codex" est conservée pour compatibilité avec le
# contrat public existant (projects/routes.py, goal_orchestrator.py -
# hors périmètre de ce correctif). En interne, elle déclenche désormais
# la génération via Ollama (_run_ollama_agent_generation), plus Codex CLI.
BuildMode = Literal["deterministic", "codex", "hybrid"]

# SENSITIVE_BUILD_KEYWORDS retirée : cette liste locale (23 termes) est
# désormais fusionnée dans goal.infra.critic_engine.SENSITIVE_KEYWORDS
# (point 1 du plan de migration), importée ci-dessus avec
# normalize_for_keyword_match.


@dataclass(slots=True)
class AgentSpec:
    kind: str
    name: str
    title: str
    goal: str
    inputs: list[str]
    outputs: list[str]
    capabilities: list[str]
    safety: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "title": self.title,
            "goal": self.goal,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "capabilities": self.capabilities,
            "safety": self.safety,
        }


class AgentBuildOrchestrator:
    def __init__(
        self,
        *,
        project_manager: ProjectManager | None = None,
        project_root: Path = DEFAULT_PROJECT_ROOT,
        workspace_agents: Path = DEFAULT_WORKSPACE_AGENTS,
        workspace_tests: Path = DEFAULT_WORKSPACE_TESTS,
        generated_agents: Path = DEFAULT_GENERATED_AGENTS,
        python_executable: str | None = None,
        runtime_check: bool = True,
        ollama_generator: OllamaAgentCodeGenerator | None = None,
        runtime_governor: Any | None = None,
        business_validator: BusinessValidator | None = None,
        agent_sandbox: AgentSandbox | None = None,
        execution_engine: GoalExecutionEngine | None = None,
    ) -> None:
        self.project_manager = project_manager or get_project_manager()
        self.project_root = project_root
        self.workspace_agents = workspace_agents
        self.workspace_tests = workspace_tests
        self.generated_agents = generated_agents
        self.python_executable = python_executable or sys.executable
        self.runtime_check = runtime_check
        self.ollama_generator = ollama_generator or OllamaAgentCodeGenerator()
        self.runtime_governor = runtime_governor
        self.execution_engine = execution_engine or GoalExecutionEngine(
            self.project_manager.sqlite_store
        )
        # AgentSandbox et BusinessValidator : rapatriés (point 3). Import
        # direct désormais — ce ne sont plus des dépendances optionnelles
        # du monolithe, mais des organes propres au service goal.
        self.agent_sandbox = agent_sandbox or AgentSandbox(
            python_executable=self.python_executable,
            project_root=self.project_root,
        )
        self.business_validator = business_validator or BusinessValidator(
            python_executable=self.python_executable,
            project_root=self.project_root,
            sandbox=self.agent_sandbox,
        )

    async def build_from_request(
        self,
        query: str,
        *,
        requested_by: str = "user",
        source_channel: str = "api",
        build_mode: BuildMode = "deterministic",
        tracking_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mode = self._normalize_build_mode(build_mode)
        codex_state = self._codex_state(mode)
        safety = self.assess_request_safety(query)
        if not safety["auto_apply_allowed"]:
            return {
                "status": "refused",
                "project": None,
                "spec": None,
                "build_executed": False,
                "reused_existing_project": False,
                "safety": safety,
                **codex_state,
                "response": self._format_refusal_response(safety),
            }

        spec = self.plan_spec(query)
        metadata = {
            **self._project_metadata(spec, query),
            **(tracking_context or {}),
        }
        existing = self.project_manager.find_existing_agent_project(
            agent_name=spec.name,
            intent_key=metadata["intent_key"],
            spec_signature=metadata["spec_signature"],
        )
        existing_needs_validation = bool(
            existing
            and existing.get("status") == "completed"
            and (
                existing.get("business_validation_status") != "passed"
                or existing.get("sandbox_status") != "passed"
                or (
                    metadata.get("internal_capability_request")
                    and not self._has_strict_internal_business_validation(existing)
                )
            )
        )
        if existing and not existing_needs_validation:
            return self._with_build_status(
                self._reuse_existing_project(existing, spec),
                codex_state,
            )

        registered_agent = (
            None
            if existing_needs_validation or metadata.get("internal_capability_request")
            else self._registered_agent_for_spec(spec, metadata)
        )
        if registered_agent:
            return self._with_build_status(
                await self._return_registered_agent(
                    spec,
                    registered_agent,
                    query=query,
                    source_channel=source_channel,
                ),
                codex_state,
            )

        metadata["build_mode"] = mode
        metadata["codex_used"] = codex_state["codex_used"]
        project = self.project_manager.create_project(
            title=spec.title,
            project_type=spec.kind,
            requested_by=requested_by,
            source_channel=source_channel,
            query=query,
            metadata=metadata,
        )
        project_id = str(project["project_id"])
        promotion_destination: Path | None = None
        promotion_snapshot: bytes | None = None
        promotion_committed = False

        try:
            self._step(project_id, "planning", "done", 10)

            agent_file = self.workspace_agents / f"{spec.name}.py"
            test_file = self.workspace_tests / f"test_{spec.name}.py"

            self._goal_step_started(metadata, "code_generation")
            if mode in {"deterministic", "hybrid"}:
                self._tools_ready_for_build = bool(
                    metadata.get("tool_creation_status") == "ready"
                    and metadata.get("required_tools")
                )
                try:
                    agent_file = self._write_agent(spec)
                finally:
                    self._tools_ready_for_build = False
                test_file = self._write_agent_test(spec, agent_file)

            if mode in {"codex", "hybrid"}:
                codex = await self._run_ollama_agent_generation(spec, agent_file, test_file)
                codex_state.update(
                    {
                        "codex_ok": codex.get("ok"),
                        "codex_error": codex.get("error"),
                        "codex_result": codex.get("result"),
                    }
                )
                if not codex.get("ok"):
                    if mode == "codex":
                        failed = self._fail(project_id, "codex", codex.get("error") or "codex_failed")
                        return self._with_build_status(failed, codex_state)
                    codex_state["codex_fallback"] = True

            created_files = [
                self._relative(path)
                for path in (agent_file, test_file)
                if path.exists()
            ]
            self.project_manager.update_project(
                project_id,
                {
                    "created_files": created_files,
                    "status": "running",
                    "metadata": {
                        **metadata,
                        **self._codex_project_metadata(codex_state),
                    },
                },
                step="code_generation",
                step_status="done",
                progress=35,
            )
            await self._publish_agent_created(
                agent_slug=spec.name,
                project_id=project_id,
                metadata=metadata,
                destination=agent_file,
                runtime_status="draft",
            )
            self._goal_step_completed(
                metadata,
                "code_generation",
                project_id=project_id,
                agent_slug=spec.name,
            )

            self._goal_step_started(metadata, "validation")
            validation = validate_agent(str(agent_file))
            if not validation.get("ok"):
                self.project_manager.update_project(
                    project_id,
                    {"validation_status": "failed"},
                )
                failed = self._fail(project_id, "validation", validation.get("error", "validation_failed"))
                return self._with_build_status(failed, codex_state)
            self.project_manager.update_project(
                project_id,
                {"validation_status": "passed"},
            )
            self._step(project_id, "validation", "done", 50)

            self._goal_step_started(metadata, "compile")
            compile_result = self._run_command(
                [self.python_executable, "-m", "py_compile", str(agent_file)],
                "compile_agent",
            )
            if compile_result["returncode"] != 0:
                self.project_manager.update_project(
                    project_id,
                    {"compile_status": "failed"},
                )
                failed = self._fail(project_id, "compile", compile_result["stderr_tail"] or "compile_failed", compile_result)
                return self._with_build_status(failed, codex_state)
            self.project_manager.update_project(
                project_id,
                {"compile_status": "passed"},
            )
            self._step(project_id, "compile", "done", 60)

            self._sandbox_started(metadata, project_id)
            self._goal_step_started(metadata, "tests")
            test_result = self._run_command(
                [self.python_executable, "-m", "pytest", "-q", str(test_file)],
                "pytest_agent",
            )
            self._append_test_result(project_id, test_result)
            sandbox_payload = test_result.get("payload")
            sandbox_test_failed = self._test_result_failed(test_result)
            if sandbox_test_failed:
                self.project_manager.update_project(
                    project_id,
                    {"test_status": "failed"},
                )
                self._sandbox_failed(metadata, project_id, test_result)
                failure_message = (
                    str(sandbox_payload.get("error") or "")
                    if isinstance(sandbox_payload, dict)
                    and sandbox_payload.get("ok") is False
                    else ""
                )
                failed = self._fail(
                    project_id,
                    "tests",
                    failure_message
                    or test_result["stdout_tail"]
                    or test_result["stderr_tail"],
                )
                return self._with_build_status(failed, codex_state)
            self.project_manager.update_project(
                project_id,
                {"test_status": "passed"},
            )
            self._step(project_id, "tests", "done", 70)

            self._goal_step_started(metadata, "business_validation")
            business_validation = self.business_validator.validate(
                spec.to_dict(),
                agent_file,
                query,
                context=metadata,
            )
            self.project_manager.update_project(
                project_id,
                {
                    "business_validation_status": business_validation["status"],
                    "business_validation_result": business_validation,
                },
            )
            if not business_validation.get("ok"):
                self._sandbox_failed(metadata, project_id, business_validation)
                errors = business_validation.get("errors") or ["business_validation_failed"]
                failed = self._fail(
                    project_id,
                    "business_validation",
                    "; ".join(str(error) for error in errors),
                )
                return self._with_build_status(failed, codex_state)
            self._step(project_id, "business_validation", "done", 75)

            sandbox_verification = self._sandbox_verify_agent(spec, agent_file)
            if not sandbox_verification.get("ok"):
                self._sandbox_failed(
                    metadata,
                    project_id,
                    sandbox_verification,
                )
                failed = self._fail(
                    project_id,
                    "sandbox",
                    str(sandbox_verification.get("error") or "sandbox_verification_failed"),
                )
                return self._with_build_status(failed, codex_state)
            self._sandbox_passed(
                metadata,
                project_id,
                sandbox_verification,
            )

            self._goal_step_started(metadata, "runtime_governor")
            governor = self._get_runtime_governor()
            promotion_snapshot = self._snapshot_promotion_target(agent_file)
            promotion = AgentPromotionService(
                generated_dir=self.generated_agents,
                runtime_governor=governor,
            ).promote(
                agent_file,
                requested_by=requested_by,
            )
            governor_allowed = promotion.get("ok") is True
            governor_policy = promotion.get("governor_policy") or governor.to_dict()
            self.project_manager.update_project(
                project_id,
                {
                    "governor_status": "allowed" if governor_allowed else "refused",
                    "governor_policy": governor_policy,
                },
                step="runtime_governor",
                step_status="done" if governor_allowed else "failed",
                progress=80,
            )
            if not governor_allowed:
                failed = self._fail(
                    project_id,
                    "runtime_governor",
                    governor_policy.get("reason") or "runtime_governor_refused",
                )
                return self._with_build_status(failed, codex_state)
            destination = Path(str(promotion["destination"]))
            promotion_destination = destination
            self._goal_step_completed(
                metadata,
                "runtime_governor",
                project_id=project_id,
                agent_slug=spec.name,
            )

            self._goal_step_started(metadata, "registry")
            await self._publish_agent_lifecycle(
                AGENT_PROMOTED,
                agent_slug=spec.name,
                project_id=project_id,
                metadata=metadata,
                path=destination,
                status="promoted",
            )

            registry = DynamicAgentRegistry(self.generated_agents)
            registry.load_generated_agents()
            registered_record = registry.find_registered_agent_for_spec(
                spec.to_dict(),
                self._spec_signature(spec),
            )

            if not destination.exists() or not registered_record:
                self._rollback_promotion(destination, promotion_snapshot)
                self._reload_runtime_after_rollback()
                failed = self._fail(
                    project_id,
                    "registry",
                    f"Agent copié mais non visible dans le registry runtime : {spec.name}",
                )
                return self._with_build_status(failed, codex_state)

            self.project_manager.update_project(
                project_id,
                {
                    "registry_status": "registered",
                    "registered_agent": spec.name,
                    "created_files": [
                        self._relative(agent_file),
                        self._relative(test_file),
                        self._relative(destination),
                    ],
                },
                step="registry",
                step_status="done",
                progress=90,
            )
            await self._publish_agent_lifecycle(
                AGENT_REGISTERED,
                agent_slug=spec.name,
                project_id=project_id,
                metadata=metadata,
                path=destination,
                status="registered",
            )
            self._goal_step_completed(
                metadata,
                "registry",
                project_id=project_id,
                agent_slug=spec.name,
            )

            self._goal_step_started(metadata, "verification")
            verification = await self._verify_agent(spec)
            if not verification.get("ok"):
                self._rollback_promotion(destination, promotion_snapshot)
                self._reload_runtime_after_rollback()
                self.project_manager.update_project(
                    project_id,
                    {
                        "registry_status": "not_registered",
                        "registered_agent": None,
                        "runtime_status": "failed",
                    },
                )
                failed = self._fail(project_id, "verification", verification.get("error", "verification_failed"))
                return self._with_build_status(failed, codex_state)

            runtime_status = "skipped" if verification.get("skipped") else "available"
            completed = self.project_manager.update_project(
                project_id,
                {
                    "status": "completed",
                    "current_step": "completed",
                    "progress": 100,
                    "result": {
                        "agent": spec.name,
                        "verification": verification,
                        "runtime_reload": verification.get("runtime_reload"),
                        "available": True,
                        **self._codex_project_metadata(codex_state),
                    },
                    "runtime_status": runtime_status,
                    "error": None,
                    "metadata": {
                        **metadata,
                        **self._codex_project_metadata(codex_state),
                    },
                },
                step="verification",
                step_status="done",
                progress=100,
            )
            self._goal_step_completed(
                metadata,
                "verification",
                project_id=project_id,
                agent_slug=spec.name,
            )
            promotion_committed = True
            return self._with_build_status(
                {
                    "status": "completed",
                    "project": completed,
                    "spec": spec.to_dict(),
                    "build_executed": True,
                    "reused_existing_project": False,
                    "runtime_reload": verification.get("runtime_reload"),
                    "response": self.format_project_response(completed),
                },
                codex_state,
            )
        except Exception as exc:
            if promotion_destination is not None and not promotion_committed:
                self._rollback_promotion(promotion_destination, promotion_snapshot)
                self._reload_runtime_after_rollback()
            failed = self._fail(project_id, "exception", str(exc))
            return self._with_build_status(
                {
                    "status": "failed",
                    "project": failed["project"],
                    "spec": spec.to_dict(),
                    "build_executed": True,
                    "reused_existing_project": False,
                    "response": self.format_project_response(failed["project"]),
                },
                codex_state,
            )

    def plan_spec(self, query: str) -> AgentSpec:
        normalized = self._normalize(query)
        explicit_name = self._explicit_agent_name(normalized)
        if explicit_name:
            return AgentSpec(
                kind="agent",
                name=explicit_name,
                title=f"Agent {explicit_name}",
                goal=query,
                inputs=["text"],
                outputs=["response"],
                capabilities=["deterministic_response"],
                safety={"filesystem": "limited", "network": "none_required"},
            )

        if "wwdc" in normalized or "apple" in normalized:
            return AgentSpec(
                kind="agent",
                name="event_countdown_agent",
                title="Agent compte a rebours WWDC",
                goal="Donner le temps restant avant la prochaine WWDC d'Apple",
                inputs=["event_name"],
                outputs=["remaining_time", "target_date", "source"],
                capabilities=["datetime", "static_event_source"],
                safety={"filesystem": "limited", "network": "none_required"},
            )
        if "meteo" in normalized or "weather" in normalized:
            agriculture_domain = self._agriculture_domain(normalized)
            if agriculture_domain:
                return AgentSpec(
                    kind="agent",
                    name=f"{agriculture_domain}_weather_agent",
                    title="Agent meteo agricole",
                    goal="Surveiller et repondre aux demandes meteo agricoles",
                    inputs=["location", "crop", "weather_question"],
                    outputs=["agriculture_weather_summary", "risk_flags", "source"],
                    capabilities=[
                        "parse_agriculture_weather_request",
                        "static_agriculture_weather_fallback",
                        "format_agriculture_weather_response",
                    ],
                    safety={"filesystem": "limited", "network": "read_only_if_available"},
                )
            return AgentSpec(
                kind="agent",
                name="weather_watch_agent",
                title="Agent de suivi meteo",
                goal="Repondre aux demandes meteo simples",
                inputs=["location"],
                outputs=["summary", "source"],
                capabilities=["parse_weather_request", "static_weather_fallback"],
                safety={"filesystem": "limited", "network": "read_only_if_available"},
            )

        kind = "tool" if any(word in normalized for word in ("tool", "outil")) else "agent"
        base = self._name_from_query(normalized)
        return AgentSpec(
            kind=kind,
            name=base,
            title=f"{kind.capitalize()} {base}",
            goal=query,
            inputs=["text"],
            outputs=["response"],
            capabilities=["deterministic_response"],
            safety={"filesystem": "limited", "network": "none_required"},
        )

    def format_project_response(self, project: dict[str, Any] | None) -> str:
        if not project:
            return "Projet introuvable."

        result = project.get("result") or {}
        tests = project.get("test_results") or []
        tests_ok = bool(tests) and all(item.get("returncode") == 0 for item in tests)
        available = bool(result.get("available"))
        registered = project.get("registry_status") == "registered" and bool(
            project.get("registered_agent")
        )
        metadata = project.get("metadata") or {}
        reused_registered = bool(metadata.get("reused_registered_agent"))
        build_executed = metadata.get("build_executed")
        reused_existing = bool(metadata.get("reused_existing_project")) or build_executed is False

        if reused_existing:
            return self._format_reused_agent_response(
                str(result.get("agent") or project.get("registered_agent") or metadata.get("agent_name") or "inconnu"),
                str(project.get("project_id") or "inconnu"),
            )

        if project.get("status") == "completed" and available and registered and (tests_ok or reused_registered):
            tests_line = (
                "Tests : non relancés (agent déjà enregistré)."
                if reused_registered and not tests_ok
                else "Tests : OK."
            )
            runtime_reload = result.get("runtime_reload") or (result.get("verification") or {}).get("runtime_reload") or {}
            runtime_line = "Runtime rechargé : OK." if runtime_reload.get("ok") else "Runtime rechargé : non vérifié."
            return (
                "Projet terminé.\n"
                f"Agent créé : {result.get('agent')}.\n"
                f"{tests_line}\n"
                "Agent enregistré : oui.\n"
                f"{runtime_line}"
            )

        if project.get("status") == "failed":
            return (
                f"Projet échoué : {project.get('project_id')}.\n"
                f"Étape : {project.get('current_step')}.\n"
                f"Cause : {project.get('error') or 'inconnue'}"
            )

        return (
            f"Projet créé : {project.get('project_id')}.\n"
            f"Type : création d’{project.get('type')}.\n"
            f"Statut : {project.get('status')}.\n"
            f"Étape actuelle : {project.get('current_step')}.\n"
            "Tu peux me demander : 'où en est le projet WWDC ?'"
        )

    def format_status_response(self, project: dict[str, Any] | None) -> str:
        if not project:
            return "Aucun projet correspondant trouvé."
        tests = project.get("test_results") or []
        tests_status = "OK" if tests and all(item.get("returncode") == 0 for item in tests) else "non vérifié"
        result = project.get("result") or {}
        available = "oui" if result.get("available") else "non"
        files = project.get("created_files") or []
        return (
            f"Projet : {project.get('project_id')}\n"
            f"Statut : {project.get('status')}\n"
            f"Étape actuelle : {project.get('current_step')}\n"
            f"Progression : {project.get('progress')}%\n"
            f"Fichiers créés : {', '.join(files) if files else 'aucun'}\n"
            f"Tests : {tests_status}\n"
            f"Agent/tool disponible : {available}\n"
            f"Erreur : {project.get('error') or 'aucune'}"
        )

    def _write_agent(self, spec: AgentSpec) -> Path:
        self.workspace_agents.mkdir(parents=True, exist_ok=True)
        path = self.workspace_agents / f"{spec.name}.py"
        business_scenario = self._deterministic_business_scenario(spec)
        if business_scenario == "easter_2027":
            content = self._easter_2027_agent_code(spec)
        elif business_scenario == "christmas":
            content = self._christmas_agent_code(spec)
        elif business_scenario == "ipv4_subnet":
            content = self._ipv4_subnet_agent_code(spec)
        elif (
            business_scenario == "neron_log_analysis"
            and getattr(self, "_tools_ready_for_build", False)
        ):
            content = self._neron_log_analysis_agent_code(spec)
        elif spec.name == "event_countdown_agent":
            content = self._wwdc_agent_code()
        elif spec.name == "weather_watch_agent":
            content = self._weather_agent_code()
        elif "parse_agriculture_weather_request" in spec.capabilities:
            content = self._agriculture_weather_agent_code(spec)
        else:
            content = self._generic_agent_code(spec)
        content = self._attach_agent_spec(content, spec)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_agent_test(self, spec: AgentSpec, agent_file: Path) -> Path:
        self.workspace_tests.mkdir(parents=True, exist_ok=True)
        path = self.workspace_tests / f"test_{spec.name}.py"
        path.write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "import importlib.util",
                    "import pathlib",
                    "import pytest",
                    "",
                    f"AGENT_FILE = pathlib.Path({str(agent_file)!r})",
                    "",
                    "def load_agent():",
                    "    spec = importlib.util.spec_from_file_location('generated_agent_under_test', AGENT_FILE)",
                    "    module = importlib.util.module_from_spec(spec)",
                    "    assert spec and spec.loader",
                    "    spec.loader.exec_module(module)",
                    "    return module.Agent()",
                    "",
                    "@pytest.mark.asyncio",
                    "async def test_agent_execute_returns_response():",
                    "    result = await load_agent().execute(text='combien de temps avant la WWDC ?')",
                    "    assert isinstance(result, dict)",
                    "    assert result.get('response')",
                    "    assert result.get('status') == 'ok'",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return path

    async def _run_ollama_agent_generation(
        self,
        spec: AgentSpec,
        agent_file: Path,
        test_file: Path,
    ) -> dict[str, Any]:
        """Génère le code de l'agent via le service llm (task_type='code').

        Remplace l'ancien flux Codex CLI. Différence structurelle
        importante : avant, le CLI écrivait lui-même agent_file ET
        test_file sur le disque (mode agentique), et on vérifiait après
        coup qu'aucune écriture parasite n'avait eu lieu (snapshot de
        generated_agents/). Ici, le modèle ne renvoie que du TEXTE — il
        ne peut écrire aucun fichier de lui-même. C'est NOUS qui écrivons
        exactement le fichier attendu, rien d'autre ne peut être touché :
        le filet de rattrapage par snapshot n'a plus lieu d'être.

        Le fichier de TEST n'est plus généré par le modèle : il réutilise
        le template déterministe existant (_write_agent_test, inchangé)
        — moins de surface, le test de validation n'est jamais façonné
        par l'IA elle-même.
        """
        self.workspace_agents.mkdir(parents=True, exist_ok=True)
        self.workspace_tests.mkdir(parents=True, exist_ok=True)

        prompt = self._ollama_agent_prompt(spec)
        try:
            code = await self.ollama_generator.generate_agent_code(
                prompt, request_id=f"agent_builder_{spec.name}"
            )
        except RuntimeError as exc:
            return {
                "ok": False,
                "error": f"ollama_agent_generation_failed:{exc}",
                "result": None,
            }

        agent_file.write_text(code, encoding="utf-8")
        self._write_agent_test(spec, agent_file)

        missing = [
            self._relative(path)
            for path in (agent_file, test_file)
            if not path.exists()
        ]
        if missing:
            return {
                "ok": False,
                "error": f"ollama_missing_files: {', '.join(missing)}",
                "result": None,
            }

        return {
            "ok": True,
            "error": None,
            "result": {"provider": "ollama", "task_type": "code"},
        }

    def _ollama_agent_prompt(self, spec: AgentSpec) -> str:
        spec_json = json.dumps(spec.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        return "\n".join(
            [
                "Mission: generate a deterministic Neron dynamic agent.",
                "",
                "Agent contract:",
                "- The file must define class Agent.",
                f"- Agent.name must be {spec.name!r}.",
                "- Agent.execute MUST have EXACTLY this signature, no other:",
                "    async def execute(self, text: str = \"\") -> dict",
                "  Do not use a different parameter name (e.g. not 'inputs', not 'payload',"
                " not 'request'). Do not add extra required parameters. The test harness"
                " that validates this agent calls it as execute(text='...') — any other"
                " signature will fail validation.",
                "- The returned dict MUST contain a key literally named \"response\" whose"
                " value is a non-empty string, on EVERY code path (success AND error),"
                " in addition to any other domain-specific keys you want to include.",
                "  Example of a correct return value (adapt the content, keep the shape):",
                "    return {\"status\": \"ok\", \"response\": \"3 mots trouves\", \"count\": 3}",
                "  This is WRONG — missing the literal \"response\" key on the success path:",
                "    return {\"status\": \"ok\", \"count\": 3, \"text\": text}",
                "- Keep behavior deterministic and testable without network access.",
                "- Do not read, write, or reference secrets, systemd, or security config.",
                "- Import nothing that performs filesystem, network, or subprocess access.",
                "",
                "Agent spec:",
                spec_json,
                "",
                "Return only the raw Python source, no explanation before or after it.",
            ]
        )

    def _snapshot_promotion_target(self, agent_file: Path) -> bytes | None:
        destination = self.generated_agents / agent_file.name
        return destination.read_bytes() if destination.exists() else None

    def _rollback_promotion(self, destination: Path, previous: bytes | None) -> None:
        if previous is None:
            destination.unlink(missing_ok=True)
            return
        destination.write_bytes(previous)

    def _reload_runtime_after_rollback(self) -> None:
        if not self.runtime_check:
            return
        from agents.runtime.runtime import get_agent_runtime

        runtime = get_agent_runtime()
        registry = runtime.registry
        if registry is not None and hasattr(registry, "generated_dir"):
            registry.generated_dir = self.generated_agents
        runtime.reload()

    def _get_runtime_governor(self) -> Any:
        if self.runtime_governor is not None:
            return self.runtime_governor
        from core.runtime.governor import get_runtime_governor

        return get_runtime_governor()

    def _registered_agent_for_spec(
        self,
        spec: AgentSpec,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        registry = DynamicAgentRegistry(self.generated_agents)
        return registry.find_registered_agent_for_spec(
            spec.to_dict(),
            metadata.get("spec_signature"),
        )

    def _project_metadata(self, spec: AgentSpec, query: str) -> dict[str, Any]:
        return {
            "spec": spec.to_dict(),
            "agent_name": spec.name,
            "intent_key": self._intent_key(spec),
            "normalized_query": self._normalize_for_key(query),
            "spec_signature": self._spec_signature(spec),
        }

    def _has_strict_internal_business_validation(
        self,
        project: dict[str, Any],
    ) -> bool:
        validation = project.get("business_validation_result") or {}
        scenario = validation.get("scenario") or {}
        context = validation.get("validation_context") or {}
        return bool(
            validation.get("ok")
            and context.get("internal_capability_request")
            and scenario.get("fallback") is False
        )

    def _reuse_existing_project(self, project: dict[str, Any], spec: AgentSpec) -> dict[str, Any]:
        return {
            "status": project.get("status") or "completed",
            "project": project,
            "spec": spec.to_dict(),
            "build_executed": False,
            "reused_existing_project": True,
            "response": self._format_reused_agent_response(
                str(project.get("registered_agent") or spec.name),
                str(project.get("project_id") or "inconnu"),
            ),
        }

    async def _return_registered_agent(
        self,
        spec: AgentSpec,
        registered_agent: dict[str, Any],
        *,
        query: str,
        source_channel: str,
    ) -> dict[str, Any]:
        verification = await self._verify_agent(spec)
        await self._publish_agent_consulted(spec, registered_agent, query, source_channel, verification)
        if not verification.get("ok"):
            return {
                "status": "failed",
                "project": None,
                "agent": registered_agent,
                "spec": spec.to_dict(),
                "build_executed": False,
                "reused_existing_agent": True,
                "reused_existing_project": False,
                "reused_registered_agent": True,
                "response": self._format_registered_agent_response(
                    spec,
                    registered_agent,
                    verification,
                ),
            }

        return {
            "status": "completed",
            "project": None,
            "agent": registered_agent,
            "spec": spec.to_dict(),
            "build_executed": False,
            "reused_existing_agent": True,
            "reused_existing_project": False,
            "reused_registered_agent": True,
            "response": self._format_registered_agent_response(
                spec,
                registered_agent,
                verification,
            ),
        }

    async def _publish_agent_consulted(
        self,
        spec: AgentSpec,
        registered_agent: dict[str, Any],
        query: str,
        source_channel: str,
        verification: dict[str, Any],
    ) -> None:
        from modules.events.event import Event
        from modules.events.event_bus import event_bus
        from modules.events.event_types import AGENT_CONSULTED

        await event_bus.publish(
            Event(
                type=AGENT_CONSULTED,
                source="agent_build_orchestrator",
                payload={
                    "agent": spec.name,
                    "query": query,
                    "source_channel": source_channel,
                    "registered_path": registered_agent.get("path"),
                    "available": bool(verification.get("ok")),
                    "reused_registered_agent": True,
                },
            )
        )

    async def _publish_agent_created(
        self,
        *,
        agent_slug: str,
        project_id: str,
        metadata: dict[str, Any],
        destination: Path,
        runtime_status: str,
    ) -> None:
        await event_bus.publish(Event(
            type=AGENT_CREATED,
            source="agent_build_orchestrator",
            payload={
                "agent_slug": agent_slug,
                "project_id": project_id,
                "goal_id": metadata.get("goal_id"),
                "plan_id": metadata.get("plan_id"),
                "path": str(destination),
                "runtime_status": runtime_status,
            },
        ))

    async def _publish_agent_lifecycle(
        self,
        event_type: str,
        *,
        agent_slug: str,
        project_id: str,
        metadata: dict[str, Any],
        path: Path,
        status: str,
    ) -> None:
        await event_bus.publish(Event(
            type=event_type,
            source="agent_build_orchestrator",
            payload={
                "agent_slug": agent_slug,
                "project_id": project_id,
                "goal_id": metadata.get("goal_id"),
                "plan_id": metadata.get("plan_id"),
                "path": str(path),
                "status": status,
            },
        ))

    def _format_registered_agent_response(
        self,
        spec: AgentSpec,
        registered_agent: dict[str, Any],
        verification: dict[str, Any],
    ) -> str:
        if not verification.get("ok"):
            return (
                f"Agent existant indisponible : {spec.name}.\n"
                f"Fichier enregistré : {registered_agent.get('path') or 'inconnu'}.\n"
                f"Cause : {verification.get('error') or 'verification_failed'}"
            )

        return (
            self._format_reused_agent_response(spec.name, "aucun projet de build")
        )

    def _format_reused_agent_response(self, agent_name: str, project_id: str) -> str:
        return (
            f"Agent déjà disponible : {agent_name}.\n"
            f"Projet existant : {project_id}.\n"
            "Tests déjà validés : OK.\n"
            "Aucune reconstruction nécessaire."
        )

    async def _verify_agent(self, spec: AgentSpec) -> dict[str, Any]:
        if not self.runtime_check:
            return {"ok": True, "skipped": True, "runtime_reload": {"ok": True, "skipped": True, "agents": [spec.name]}}
        from agents.runtime.runtime import get_agent_runtime

        runtime = get_agent_runtime()
        registry = runtime.registry
        if registry is not None and hasattr(registry, "generated_dir"):
            registry.generated_dir = self.generated_agents
        runtime_reload = runtime.reload()
        execution = await runtime.run_agent(spec.name, "combien de temps avant la WWDC ?")
        result = execution.to_dict()
        if not execution.ok:
            return {"ok": False, "error": execution.error, "raw": result, "runtime_reload": runtime_reload}
        response = execution.response
        return {
            "ok": bool(response),
            "response": response,
            "raw": result,
            "runtime_reload": runtime_reload,
        }

    def _run_command(self, command: list[str], name: str) -> dict[str, Any]:
        if name.startswith("pytest_agent"):
            return self.agent_sandbox.run_pytest(
                command[-1],
                timeout=120,
                name=name,
            )
        completed = subprocess.run(
            command,
            cwd=self.project_root,
            text=True,
            capture_output=True,
            timeout=120,
        )
        return {
            "name": name,
            "command": command,
            "returncode": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _test_result_failed(result: dict[str, Any]) -> bool:
        payload = result.get("payload")
        return bool(
            result.get("returncode") != 0
            or (
                isinstance(payload, dict)
                and payload.get("ok") is False
            )
        )

    def _sandbox_verify_agent(
        self,
        spec: AgentSpec,
        agent_file: Path,
    ) -> dict[str, Any]:
        execution = self.agent_sandbox.execute_agent(
            agent_file,
            "combien de temps avant la WWDC ?",
            name="sandbox_verification",
        )
        if not execution.get("ok"):
            return execution
        raw = execution.get("result")
        response = ""
        if isinstance(raw, dict):
            response = str(
                raw.get("response")
                or raw.get("content")
                or raw.get("result")
                or raw.get("answer")
                or ""
            ).strip()
            status = str(raw.get("status") or "").lower()
            if status and status not in {"ok", "success", "passed"}:
                return {
                    "ok": False,
                    "error": f"sandbox_agent_status_not_ok: {raw.get('status')}",
                    "sandbox": execution.get("sandbox"),
                }
        else:
            response = str(raw or "").strip()
        if not response:
            return {
                "ok": False,
                "error": "sandbox_empty_response",
                "sandbox": execution.get("sandbox"),
            }
        return {
            "ok": True,
            "response": response,
            "sandbox": execution.get("sandbox"),
            "agent": spec.name,
        }

    def _sandbox_started(
        self,
        metadata: dict[str, Any],
        project_id: str,
    ) -> None:
        diagnostics = self._sandbox_diagnostics()
        self.project_manager.update_project(
            project_id,
            {
                "sandbox_status": "running",
                "sandbox_backend": diagnostics.get("backend_used"),
                "sandbox_isolation_level": diagnostics.get("isolation_level"),
            },
        )
        goal_id = str(metadata.get("goal_id") or "")
        if goal_id:
            event_payload = {
                "project_id": project_id,
                **diagnostics,
            }
            self.execution_engine.mark_sandbox_backend_event(
                goal_id,
                "sandbox_backend_selected",
                event_payload,
            )
            if diagnostics.get("backend_used") == "systemd":
                self.execution_engine.mark_sandbox_backend_event(
                    goal_id,
                    "sandbox_systemd_started",
                    event_payload,
                )
            elif diagnostics.get("fallback_reason"):
                self.execution_engine.mark_sandbox_backend_event(
                    goal_id,
                    "sandbox_fallback_python",
                    event_payload,
                )
            self.execution_engine.mark_sandbox_started(
                goal_id,
                event_payload,
            )

    def _sandbox_passed(
        self,
        metadata: dict[str, Any],
        project_id: str,
        result: dict[str, Any],
    ) -> None:
        diagnostics = self._sandbox_result_diagnostics(result)
        self.project_manager.update_project(
            project_id,
            {
                "sandbox_status": "passed",
                "sandbox_result": result,
                "sandbox_backend": diagnostics.get("backend_used"),
                "sandbox_isolation_level": diagnostics.get("isolation_level"),
            },
            step="sandbox",
            step_status="done",
            progress=78,
        )
        goal_id = str(metadata.get("goal_id") or "")
        if goal_id:
            self.execution_engine.mark_sandbox_passed(
                goal_id,
                {
                    "project_id": project_id,
                    "agent_slug": metadata.get("agent_name"),
                },
            )

    def _sandbox_failed(
        self,
        metadata: dict[str, Any],
        project_id: str,
        result: dict[str, Any],
    ) -> None:
        diagnostics = self._sandbox_result_diagnostics(result)
        self.project_manager.update_project(
            project_id,
            {
                "sandbox_status": "failed",
                "sandbox_result": result,
                "sandbox_backend": diagnostics.get("backend_used"),
                "sandbox_isolation_level": diagnostics.get("isolation_level"),
            },
        )
        goal_id = str(metadata.get("goal_id") or "")
        if goal_id:
            if diagnostics.get("backend_used") == "systemd" and (
                result.get("backend_error")
                or (result.get("sandbox") or {}).get("backend_error")
            ):
                self.execution_engine.mark_sandbox_backend_event(
                    goal_id,
                    "sandbox_systemd_failed",
                    {
                        "project_id": project_id,
                        **diagnostics,
                        "error": result.get("error"),
                    },
                )
            self.execution_engine.mark_sandbox_failed(
                goal_id,
                str(result.get("error") or "agent_sandbox_failed"),
                {"project_id": project_id, **diagnostics},
            )

    def _sandbox_diagnostics(self) -> dict[str, Any]:
        diagnostics = getattr(self.agent_sandbox, "diagnostics", None)
        if callable(diagnostics):
            return dict(diagnostics())
        return {}

    def _sandbox_result_diagnostics(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        sandbox = result.get("sandbox") or result
        diagnostics = self._sandbox_diagnostics()
        for key in (
            "backend_selected",
            "backend_used",
            "isolation_level",
            "systemd_available",
            "user_available",
            "sudo_used",
            "sudo_available",
            "sudo_error",
            "systemd_run_path",
            "fallback_reason",
        ):
            if sandbox.get(key) is not None:
                diagnostics[key] = sandbox[key]
        return diagnostics

    def _append_test_result(self, project_id: str, result: dict[str, Any]) -> None:
        project = self.project_manager.get_project(project_id)
        if not project:
            return
        tests = list(project.get("test_results") or [])
        tests.append(result)
        self.project_manager.update_project(project_id, {"test_results": tests})

    def _step(self, project_id: str, step: str, status: str, progress: int) -> None:
        project = self.project_manager.update_project(
            project_id,
            {"status": "running"},
            step=step,
            step_status=status,
            progress=progress,
        )
        if step != "planning" and project:
            self._goal_step_completed(
                project.get("metadata") or {},
                step,
                project_id=project_id,
                agent_slug=(project.get("metadata") or {}).get("agent_name"),
            )

    def _fail(
        self,
        project_id: str,
        step: str,
        error: str,
        test_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if test_result:
            self._append_test_result(project_id, test_result)
        existing = self.project_manager.get_project(project_id) or {}
        generated_prefix = self._relative(self.generated_agents).rstrip("/") + "/"
        created_files = [
            path
            for path in existing.get("created_files") or []
            if not str(path).replace("\\", "/").startswith(generated_prefix)
        ]
        project = self.project_manager.update_project(
            project_id,
            {
                "status": "failed",
                "current_step": step,
                "registry_status": "not_registered",
                "registered_agent": None,
                "created_files": created_files,
                "runtime_status": "failed" if step == "verification" else "not_available",
                "result": {"available": False},
            },
            step=step,
            step_status="failed",
            error=error,
        )
        goal_id = str((existing.get("metadata") or {}).get("goal_id") or "")
        if goal_id:
            self.execution_engine.mark_step_failed(goal_id, step, error)
        return {"status": "failed", "project": project}

    def _goal_step_started(
        self,
        metadata: dict[str, Any],
        step: str,
    ) -> None:
        goal_id = str(metadata.get("goal_id") or "")
        if goal_id:
            self.execution_engine.mark_step_started(goal_id, step)

    def _goal_step_completed(
        self,
        metadata: dict[str, Any],
        step: str,
        *,
        project_id: str,
        agent_slug: str | None,
    ) -> None:
        goal_id = str(metadata.get("goal_id") or "")
        if goal_id:
            self.execution_engine.mark_step_completed(
                goal_id,
                step,
                {
                    "project_id": project_id,
                    "agent_slug": agent_slug,
                },
            )

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.project_root.resolve()))
        except ValueError:
            return str(path)

    def _normalize(self, value: str) -> str:
        text = unicodedata.normalize("NFKD", value.lower())
        text = "".join(char for char in text if unicodedata.category(char) != "Mn")
        return " ".join(text.split())

    def _normalize_for_key(self, value: str) -> str:
        text = self._normalize(value)
        cleaned = []
        for char in text:
            cleaned.append(char if char.isalnum() else " ")
        return " ".join("".join(cleaned).split())

    def _deterministic_business_scenario(self, spec: AgentSpec) -> str | None:
        searchable = self._normalize(
            " ".join(
                [
                    spec.goal,
                    spec.name,
                    spec.title,
                    " ".join(spec.capabilities),
                ]
            )
        )
        if ("paques 2027" in searchable or "easter 2027" in searchable):
            return "easter_2027"
        if "noel" in searchable or "christmas" in searchable:
            return "christmas"
        if (
            "subnet ipv4" in searchable
            or "ipv4 subnet" in searchable
            or ("subnet" in searchable and "ipv4" in searchable)
            or "reseau ipv4" in searchable
        ):
            return "ipv4_subnet"
        if "log" in searchable or "journal" in searchable:
            return "neron_log_analysis"
        return None

    def _normalize_build_mode(self, build_mode: str) -> BuildMode:
        if build_mode in {"deterministic", "codex", "hybrid"}:
            return build_mode  # type: ignore[return-value]
        return "deterministic"

    def _codex_state(self, build_mode: BuildMode) -> dict[str, Any]:
        return {
            "build_mode": build_mode,
            "codex_used": build_mode in {"codex", "hybrid"},
            "codex_ok": None,
            "codex_fallback": False,
            "codex_error": None,
        }

    def _codex_project_metadata(self, codex_state: dict[str, Any]) -> dict[str, Any]:
        return {
            "build_mode": codex_state.get("build_mode"),
            "codex_used": codex_state.get("codex_used"),
            "codex_ok": codex_state.get("codex_ok"),
            "codex_fallback": codex_state.get("codex_fallback"),
            "codex_error": codex_state.get("codex_error"),
        }

    def _with_build_status(
        self,
        payload: dict[str, Any],
        codex_state: dict[str, Any],
    ) -> dict[str, Any]:
        payload.update(self._codex_project_metadata(codex_state))
        if "codex_result" in codex_state:
            payload["codex_result"] = codex_state.get("codex_result")
        return payload

    def assess_request_safety(self, query: str) -> dict[str, Any]:
        # Utilise désormais la normalisation ET la liste de mots-clés
        # PARTAGÉES avec CriticEngine et GoalOrchestrator (point 1 du plan
        # de migration) — plus de liste locale qui pourrait diverger.
        normalized = normalize_for_keyword_match(query)
        reasons = [
            keyword
            for keyword in sorted(SENSITIVE_KEYWORDS)
            if keyword in normalized
        ]
        return {
            "auto_apply_allowed": not reasons,
            "risk_level": "low" if not reasons else "critical",
            "reasons": reasons,
        }

    def _format_refusal_response(self, safety: dict[str, Any]) -> str:
        reasons = ", ".join(str(item) for item in safety.get("reasons") or [])
        return (
            "Création d’agent refusée en mode automatique.\n"
            "Risque : critical.\n"
            f"Raison : {reasons or 'demande sensible'}.\n"
            "Aucun agent n’a été généré ni enregistré."
        )

    def _agriculture_domain(self, normalized: str) -> str | None:
        agriculture_terms = {
            "agriculture",
            "agricole",
            "agricoles",
            "agri",
            "culture",
            "cultures",
            "champ",
            "champs",
            "irrigation",
            "recolte",
            "recoltes",
            "crop",
            "crops",
            "farm",
            "farming",
        }
        if any(term in normalized.split() for term in agriculture_terms):
            return "agriculture"
        return None

    def _intent_key(self, spec: AgentSpec) -> str:
        return self._normalize_for_key(
            " ".join(
                [
                    spec.kind,
                    spec.name,
                    spec.title,
                    spec.goal,
                    " ".join(spec.inputs),
                    " ".join(spec.outputs),
                    " ".join(spec.capabilities),
                ]
            )
        )

    def _spec_signature(self, spec: AgentSpec) -> str:
        return self._normalize_for_key(
            json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False)
        )

    def _attach_agent_spec(self, content: str, spec: AgentSpec) -> str:
        spec_payload = spec.to_dict()
        if (
            self._deterministic_business_scenario(spec) == "neron_log_analysis"
            and getattr(self, "_tools_ready_for_build", False)
        ):
            spec_payload["tools"] = [
                "neron_log_reader_tool",
                "neron_log_error_filter_tool",
                "neron_log_summary_tool",
            ]
        spec_json = json.dumps(
            spec_payload,
            indent=4,
            sort_keys=True,
            ensure_ascii=False,
        )
        spec_signature = self._normalize_for_key(
            json.dumps(spec_payload, sort_keys=True, ensure_ascii=False)
        )
        metadata = (
            f"AGENT_SPEC = {spec_json}\n"
            f"AGENT_SPEC_SIGNATURE = {spec_signature!r}\n\n"
        )
        marker = "from __future__ import annotations\n\n"
        if content.startswith(marker):
            return content.replace(marker, marker + metadata, 1)
        return metadata + content

    def _name_from_query(self, normalized: str) -> str:
        ignored = {"cree", "creer", "agent", "tool", "outil", "ajoute", "un", "une", "qui", "me", "pour"}
        words = [re.sub(r"[^a-z0-9]", "", word) for word in normalized.split()]
        useful = [word for word in words if word and word not in ignored]
        base = "_".join(useful[:3]) or "custom"
        return base if base.endswith("_agent") else f"{base}_agent"

    def _explicit_agent_name(self, normalized: str) -> str | None:
        patterns = (
            r"\bnomme\s+([a-z0-9_][a-z0-9_-]*)",
            r"\bappele\s+([a-z0-9_][a-z0-9_-]*)",
            r"\bappelle\s+([a-z0-9_][a-z0-9_-]*)",
            r"\bs\s+appelle\s+([a-z0-9_][a-z0-9_-]*)",
            r"\bnamed\s+([a-z0-9_][a-z0-9_-]*)",
            r"\bname\s+([a-z0-9_][a-z0-9_-]*)",
        )

        for pattern in patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            candidate = re.sub(r"[^a-z0-9_]+", "_", match.group(1).lower()).strip("_")
            if not candidate:
                continue
            if candidate[0].isdigit():
                candidate = f"agent_{candidate}"
            return candidate

        return None

    def _wwdc_agent_code(self) -> str:
        return '''from __future__ import annotations

from datetime import datetime, timezone


class Agent:
    name = "event_countdown_agent"
    target_event = "WWDC"
    target_date = datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc)
    source = "static_fallback: Apple WWDC 2026 keynote expected June 8, 2026"

    async def execute(self, text: str = "") -> dict:
        now = datetime.now(timezone.utc)
        delta = self.target_date - now
        total_seconds = int(delta.total_seconds())
        if total_seconds <= 0:
            remaining = "l'événement est commencé ou passé"
        else:
            days, rem = divmod(total_seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, _ = divmod(rem, 60)
            remaining = f"{days} jours, {hours} heures et {minutes} minutes"
        return {
            "status": "ok",
            "agent": self.name,
            "event_name": self.target_event,
            "target_date": self.target_date.isoformat(),
            "remaining_time": remaining,
            "source": self.source,
            "response": f"Temps restant avant la WWDC : {remaining}. Date cible : {self.target_date.isoformat()}",
        }
'''

    def _weather_agent_code(self) -> str:
        return '''from __future__ import annotations


class Agent:
    name = "weather_watch_agent"

    async def execute(self, text: str = "") -> dict:
        return {
            "status": "ok",
            "agent": self.name,
            "source": "static_fallback",
            "response": "Agent météo disponible. Connecteur météo externe non configuré; réponse fallback active.",
        }
'''

    def _agriculture_weather_agent_code(self, spec: AgentSpec) -> str:
        return f'''from __future__ import annotations


class Agent:
    name = {spec.name!r}
    goal = {spec.goal!r}
    capabilities = {spec.capabilities!r}

    async def execute(self, text: str = "") -> dict:
        request = self.parse_agriculture_weather_request(text)
        fallback = self.static_agriculture_weather_fallback(request)
        response = self.format_agriculture_weather_response(fallback)
        return {{
            "status": "ok",
            "agent": self.name,
            "goal": self.goal,
            "capabilities": self.capabilities,
            "request": request,
            "source": "static_agriculture_weather_fallback",
            "response": response,
        }}

    def parse_agriculture_weather_request(self, text: str) -> dict:
        return {{
            "raw": text,
            "domain": "agriculture",
            "needs": ["meteo_agricole", "risque_cultures", "fenetre_intervention"],
        }}

    def static_agriculture_weather_fallback(self, request: dict) -> dict:
        return {{
            "summary": "Agent météo agricole disponible. Connecteur météo externe non configuré; fallback agricole actif.",
            "risk_flags": ["surveiller gel", "surveiller pluie forte", "adapter irrigation"],
            "request": request,
        }}

    def format_agriculture_weather_response(self, data: dict) -> str:
        return (
            data["summary"]
            + " Spécialisation agriculture : aide aux décisions météo pour cultures, irrigation et fenêtres de travaux."
        )
'''

    def _easter_2027_agent_code(self, spec: AgentSpec) -> str:
        return f'''from __future__ import annotations


class Agent:
    name = {spec.name!r}

    async def execute(self, text: str = "") -> dict:
        return {{
            "status": "ok",
            "agent": self.name,
            "date": "2027-03-28",
            "response": "Pâques 2027 tombe le 28 mars 2027.",
        }}
'''

    def _christmas_agent_code(self, spec: AgentSpec) -> str:
        default_year_match = re.search(r"\b(20\d{2})\b", spec.goal)
        default_year = (
            int(default_year_match.group(1))
            if default_year_match
            else None
        )
        return f'''from __future__ import annotations

import re
from datetime import date


class Agent:
    name = {spec.name!r}
    default_year = {default_year!r}

    async def execute(self, text: str = "") -> dict:
        current_date = date.today()
        year_match = re.search(r"\\b(20\\d{{2}})\\b", text)
        if year_match:
            target_year = int(year_match.group(1))
        elif self.default_year is not None:
            target_year = self.default_year
        else:
            target_year = current_date.year
            if current_date > date(target_year, 12, 25):
                target_year += 1

        christmas = date(target_year, 12, 25)
        days_remaining = (christmas - current_date).days
        response = (
            f"Noël {{target_year}} tombe le 25/12/{{target_year}}. "
            f"Écart depuis aujourd'hui : {{days_remaining}} jours."
        )
        return {{
            "status": "ok",
            "agent": self.name,
            "target_date": christmas.isoformat(),
            "days_remaining": days_remaining,
            "response": response,
        }}
'''

    def _ipv4_subnet_agent_code(self, spec: AgentSpec) -> str:
        return f'''from __future__ import annotations

import ipaddress
import re


class Agent:
    name = {spec.name!r}

    async def execute(self, text: str = "") -> dict:
        network = self._extract_network(text)
        if network is None:
            response = "Fournissez une adresse IPv4 au format CIDR, par exemple 192.168.1.42/24."
            return {{
                "status": "ok",
                "agent": self.name,
                "response": response,
            }}

        response = (
            f"Réseau: {{network.network_address}}/{{network.prefixlen}}; "
            f"masque: {{network.netmask}}; broadcast: {{network.broadcast_address}}."
        )
        return {{
            "status": "ok",
            "agent": self.name,
            "network": str(network),
            "response": response,
        }}

    def _extract_network(self, text: str) -> ipaddress.IPv4Network | None:
        for candidate in re.findall(r"\\b(?:\\d{{1,3}}\\.){{3}}\\d{{1,3}}/\\d{{1,2}}\\b", text):
            try:
                network = ipaddress.ip_network(candidate, strict=False)
            except ValueError:
                continue
            if isinstance(network, ipaddress.IPv4Network):
                return network
        return None
'''

    def _generic_agent_code(self, spec: AgentSpec) -> str:
        explicit_response = self._explicit_response_contract(spec.goal)
        if explicit_response:
            return f'''from __future__ import annotations


class Agent:
    name = {spec.name!r}

    async def execute(self, text: str = "") -> dict:
        return {{
            "status": "ok",
            "agent": self.name,
            "response": {explicit_response!r},
        }}
'''
        return f'''from __future__ import annotations


class Agent:
    name = {spec.name!r}

    async def execute(self, text: str = "") -> dict:
        request = text.strip() or {spec.goal!r}
        return {{
            "status": "ok",
            "agent": self.name,
            "response": f"Demande traitée : {{request}}",
        }}
'''

    def _explicit_response_contract(self, goal: str) -> str | None:
        match = re.search(
            r"\b(?:repond|répond|reply|returns?)\s+(?:avec\s+)?(.+?)\s*$",
            goal.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        response = match.group(1).strip(" \t\r\n\"'«».,;:")
        if self._normalize(response).startswith("a cette demande"):
            return None
        return response or None

    def _neron_log_analysis_agent_code(self, spec: AgentSpec) -> str:
        return f'''from __future__ import annotations

import re


ERROR_PATTERN = re.compile(
    r"\\b(ERROR|CRITICAL|FATAL|Traceback|Exception)\\b",
    re.IGNORECASE,
)


class Agent:
    name = {spec.name!r}

    async def execute(self, text: str = "", tools=None, context=None) -> dict:
        if tools:
            payload = dict(getattr(context, "context", {{}}) or {{}})
            reader = await tools["neron_log_reader_tool"].execute(payload)
            if not reader.ok:
                raise RuntimeError(reader.error or "log_reader_failed")
            filtered = await tools["neron_log_error_filter_tool"].execute(
                {{"logs": reader.data.get("logs", [])}}
            )
            if not filtered.ok:
                raise RuntimeError(filtered.error or "log_filter_failed")
            summary = await tools["neron_log_summary_tool"].execute(
                {{"errors": filtered.data.get("errors", [])}}
            )
            if not summary.ok:
                raise RuntimeError(summary.error or "log_summary_failed")
            return {{
                "status": "ok",
                "agent": self.name,
                "response": summary.response,
                **summary.data,
            }}

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        errors = [line for line in lines if ERROR_PATTERN.search(line)]
        if not errors:
            response = "Analyse des logs Néron : aucune erreur critique détectée."
        else:
            severity = "CRITICAL" if any(
                token in line.upper()
                for line in errors
                for token in ("CRITICAL", "FATAL")
            ) else "ERROR"
            excerpt = errors[0][:180]
            response = (
                f"Analyse des logs Néron : {{len(errors)}} erreur(s), "
                f"gravité {{severity}}. Extrait : {{excerpt}}. "
                "Recommandation : vérifier le composant concerné."
            )
        return {{
            "status": "ok",
            "agent": self.name,
            "response": response,
            "error_count": len(errors),
        }}
'''


async def build_agent_from_request(
    query: str,
    *,
    requested_by: str = "user",
    source_channel: str = "api",
    build_mode: BuildMode = "deterministic",
) -> dict[str, Any]:
    return await AgentBuildOrchestrator().build_from_request(
        query,
        requested_by=requested_by,
        source_channel=source_channel,
        build_mode=build_mode,
    )
