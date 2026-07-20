from __future__ import annotations

import ast
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path

from server.common.paths import NERON_WORKSPACE_DIR
from server.common.llm_client import LLMClient, LLMClientError, get_llm_client
from goal.infra.llm_text import strip_code_fence
from goal.tools.models import ToolNeed, ToolSpec
from goal.tools.templates import (
    codex_prompt,
    deterministic_tool_code,
    deterministic_tool_test,
)


logger = logging.getLogger("goal.tools.code_generator")


FORBIDDEN_IMPORTS = {
    "os",
    "pathlib",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {
    "__import__",
    "compile",
    "eval",
    "exec",
    "open",
}


class ToolCodeGenerator(ABC):
    @abstractmethod
    def can_generate(self, need: ToolNeed) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def generate_tool_code(self, spec: ToolSpec, need: ToolNeed) -> str:
        raise NotImplementedError

    @abstractmethod
    async def generate_tests(self, spec: ToolSpec, need: ToolNeed) -> str:
        raise NotImplementedError


class DeterministicToolCodeGenerator(ToolCodeGenerator):
    SUPPORTED_INTENTS = {
        "analyze",
        "summarize",
        "count",
        "compare",
        "diagnose",
        "monitor",
        "notify",
        "calculate",
        "search",
        "execute",
    }

    def can_generate(self, need: ToolNeed) -> bool:
        return need.intent in self.SUPPORTED_INTENTS

    async def generate_tool_code(self, spec: ToolSpec, need: ToolNeed) -> str:
        return deterministic_tool_code(spec, need)

    async def generate_tests(self, spec: ToolSpec, need: ToolNeed) -> str:
        path = str(need.metadata.get("tool_path") or f"{spec.slug}.py")
        return deterministic_tool_test(spec, path)


class OllamaToolCodeGenerator(ToolCodeGenerator):
    """Génère le code d'un outil via le service llm (task_type='code').

    Remplace l'ancien CodexToolCodeGenerator : même prompt (codex_prompt,
    déjà conçu comme un texte autonome, un seul aller-retour — jamais
    d'édition de fichiers autonome), même extraction (goal.infra.llm_text.strip_code_fence),
    seul le transport change (HTTP interne au lieu d'un CLI externe).
    task_type='code' est garanti rester sur Ollama par le plancher de
    sécurité du service llm (voir server/llm/core/router.py) — aucun
    appel ne sort de la machine, sauf reconfiguration future explicite
    et délibérée de ce service, hors du contrôle de goal.
    """

    MAX_ATTEMPTS = 2

    def __init__(
        self,
        *,
        workspace: Path = NERON_WORKSPACE_DIR / "tools",
        client: LLMClient | None = None,
    ) -> None:
        self.workspace = workspace
        self.client = client or get_llm_client()

    def can_generate(self, need: ToolNeed) -> bool:
        return True

    async def _generate(self, prompt: str, *, request_id: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                raw = await self.client.generate_code(prompt, request_id=request_id)
            except LLMClientError as exc:
                last_error = exc
                logger.warning(
                    "llm_generation_failed request_id=%s attempt=%s/%s error=%s",
                    request_id, attempt, self.MAX_ATTEMPTS, exc,
                )
                continue
            code = strip_code_fence(raw)
            if code.strip():
                return code
            last_error = RuntimeError("llm_empty_response")
        raise RuntimeError(f"ollama_tool_generation_failed:{last_error}")

    async def generate_tool_code(self, spec: ToolSpec, need: ToolNeed) -> str:
        return await self._generate(
            codex_prompt(spec, need, artifact="implementation"),
            request_id=f"tool_{spec.slug}",
        )

    async def generate_tests(self, spec: ToolSpec, need: ToolNeed) -> str:
        return await self._generate(
            codex_prompt(spec, need, artifact="unit test"),
            request_id=f"tool_test_{spec.slug}",
        )


def validate_generated_code(code: str) -> list[str]:
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"invalid_python:{exc.msg}"]

    has_execute = False
    has_tool_result = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in FORBIDDEN_IMPORTS:
                    errors.append(f"forbidden_import:{root}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root in FORBIDDEN_IMPORTS:
                errors.append(f"forbidden_import:{root}")
            if node.module == "goal.tools.models" and any(
                alias.name == "ToolResult" for alias in node.names
            ):
                has_tool_result = True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "execute":
                has_execute = True
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                errors.append(f"forbidden_call:{node.func.id}")
    if not has_execute:
        errors.append("execute_function_required")
    if not has_tool_result:
        errors.append("tool_result_required")
    return sorted(set(errors))


def validate_workspace_path(path: Path, workspace: Path) -> bool:
    try:
        path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return False
    return True



