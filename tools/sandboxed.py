"""Exécution isolée des outils générés.

Les outils générés (fichiers Python écrits par Néron, par un modèle ou par
Codex) étaient chargés dans le processus du service (importlib) : ils
s'exécutaient avec tous ses secrets, ses droits et son réseau. Leurs tests
tournaient dans un pytest qui héritait de tout l'environnement, secrets
compris. La validation AST (code_generator.validate_generated_code) n'est
qu'un filtre : elle se contourne (importlib, io.open, builtins...).

Désormais, tout passe par le bac à sable (goal.infra.agent_sandbox) : environnement
minimal sans secrets, et isolation systemd (utilisateur neron-agent) ou
bubblewrap, avec secrets.env, les données, la base mémoire et le dossier
personnel masqués. Sans isolation réelle disponible, l'exécution est refusée.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable


def _sandbox():
    from goal.infra.agent_sandbox import AgentSandbox

    return AgentSandbox()


def _declares_execute(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute"
        for node in tree.body
    )


def sandboxed_handler(path: Path | str) -> Callable[[dict[str, Any]], Awaitable[Any]]:
    """Fonction d'exécution d'un outil généré, sans jamais l'importer ici.

    Lève RuntimeError si le fichier ne définit pas `execute` (contrôle
    statique, sans exécuter le code).
    """
    tool_path = Path(path).resolve()
    if not tool_path.is_file():
        raise RuntimeError("generated_tool_not_found")
    if not _declares_execute(tool_path):
        raise RuntimeError("generated_tool_execute_missing")

    async def handler(payload: dict[str, Any]) -> Any:
        outcome = await asyncio.to_thread(_sandbox().execute_tool, tool_path, payload)
        if not outcome.get("ok"):
            return {"ok": False, "error": str(outcome.get("error") or "tool_sandbox_failed")}
        return outcome.get("result")

    handler.__name__ = f"sandboxed_{tool_path.stem}"
    return handler


def run_tool_tests(test_path: Path | str, *, timeout: int = 120) -> None:
    """Tests d'un outil généré, dans le bac à sable. Lève RuntimeError si échec."""
    result = _sandbox().run_pytest(test_path, timeout=timeout, name="pytest_generated_tool")
    if result.get("returncode") != 0:
        detail = result.get("stdout_tail") or result.get("error") or result.get("stderr_tail") or ""
        raise RuntimeError(f"tool_tests_failed:{str(detail)[-2000:]}")
