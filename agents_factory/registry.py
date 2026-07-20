from __future__ import annotations

# Rapatrié (phase 4). Chemin de données volontairement PARTAGÉ avec le
# monolithe tant que build_orchestrator.py (qui écrit ici) n'a pas migré
# (phase 8) — voir note dans le plan de migration.

import ast
import hashlib
import json
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from server.common.paths import NERON_DATA_DIR

AGENT_REGISTRY = {}
PRODUCTION_GENERATED_AGENTS = NERON_DATA_DIR / "generated_agents"
DEFAULT_GENERATED_AGENTS = Path(
    os.getenv(
        "NERON_GENERATED_AGENTS_DIR",
        str(PRODUCTION_GENERATED_AGENTS),
    )
)


class DynamicAgentRegistry:

    def __init__(self, generated_dir: Path | str | None = None):
        self.generated_dir = Path(generated_dir or DEFAULT_GENERATED_AGENTS)
        self._records: dict[str, dict[str, Any]] = {}
        self._invalid_records: dict[str, dict[str, Any]] = {}

    def load_generated_agents(self):
        AGENT_REGISTRY.clear()
        self._records.clear()
        self._invalid_records.clear()

        if not self.generated_dir.exists():
            return AGENT_REGISTRY

        for file in self.generated_dir.glob("*.py"):

            if file.name.startswith("_"):
                continue

            module_name = file.stem

            try:
                record = self._parse_record(module_name, file)
            except (OSError, SyntaxError, ValueError) as exc:
                self._record_invalid(file, f"static_validation_failed:{exc}")
                continue

            AGENT_REGISTRY[module_name] = record
            self._records[module_name] = record

        return AGENT_REGISTRY

    def find_registered_agent_for_spec(
        self,
        spec: dict[str, Any],
        spec_signature: str | None = None,
    ) -> dict[str, Any] | None:
        self.load_generated_agents()

        desired_name = self._normalize(str(spec.get("name") or ""))
        desired_signature = spec_signature or self.spec_signature(spec)

        for record in self._records.values():
            registered_signature = str(record.get("spec_signature") or "")
            if desired_signature and registered_signature and registered_signature == desired_signature:
                return record

            registered_name = self._normalize(str(record.get("agent_name") or record.get("module_name") or ""))
            has_registered_spec = bool(record.get("spec"))
            if desired_name and registered_name == desired_name and not has_registered_spec:
                return record

        return None

    def _parse_record(self, module_name: str, path: Path) -> dict[str, Any]:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        module_values = self._literal_assignments(tree.body)
        agent_class = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "Agent"
            ),
            None,
        )
        if agent_class is None:
            raise ValueError("class_agent_missing")
        execute = next(
            (
                node
                for node in agent_class.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute"
            ),
            None,
        )
        if execute is None:
            raise ValueError("async_execute_contract_missing")
        class_values = self._literal_assignments(agent_class.body)
        spec_value = (
            module_values.get("AGENT_SPEC")
            or class_values.get("agent_spec")
            or class_values.get("spec")
        )
        spec = dict(spec_value) if isinstance(spec_value, dict) else None
        agent_name = str(class_values.get("name") or module_name)
        spec_signature = str(module_values.get("AGENT_SPEC_SIGNATURE") or "")
        if not spec_signature and spec:
            spec_signature = self.spec_signature(spec)
        return {
            "module_name": module_name,
            "agent_name": agent_name,
            "path": str(path),
            "spec": spec,
            "spec_signature": spec_signature,
            "match_text": self._match_text(
                module_name,
                agent_name,
                module_values,
                class_values,
                spec,
            ),
        }

    @staticmethod
    def _literal_assignments(nodes: list[ast.stmt]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for node in nodes:
            targets: list[ast.expr]
            value_node: ast.expr | None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value_node = node.value
            else:
                continue
            if value_node is None:
                continue
            try:
                value = ast.literal_eval(value_node)
            except (ValueError, TypeError):
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    values[target.id] = value
        return values

    def list_agent_records(self) -> list[dict[str, Any]]:
        self.load_generated_agents()
        return list(self._records.values())

    def validation_index(self) -> dict[str, Any]:
        self.load_generated_agents()
        scanned_at = datetime.now(timezone.utc).isoformat()
        agents: dict[str, dict[str, Any]] = {}
        for name, record in self._records.items():
            path = Path(str(record["path"]))
            agents[name] = {
                "agent_name": str(record.get("agent_name") or name),
                "path": str(path),
                "status": "active",
                "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "last_scanned_at": scanned_at,
                "validation_ok": True,
                "error": None,
                "source": "dynamic_registry",
            }
        agents.update(self._invalid_records)
        return {
            "agents": dict(sorted(agents.items())),
            "last_scanned_at": scanned_at,
            "source": "dynamic_registry",
        }

    def scan(self) -> dict[str, Any]:
        index = self.validation_index()
        records = list(index["agents"].values())
        return {
            "status": "ok",
            "scanned": len(records),
            "active": sum(item.get("status") == "active" for item in records),
            "invalid": sum(item.get("status") == "invalid" for item in records),
            "runtime_reloaded": False,
            "runtime_reload": None,
            "stale_removed": [],
            "agents": records,
            "source": "dynamic_registry",
        }

    def diagnose_consistency(
        self,
        *,
        projects: Iterable[dict[str, Any]] = (),
        workspace_agents: Path | str | None = None,
    ) -> dict[str, Any]:
        self.load_generated_agents()
        generated_files = {
            path.stem: path
            for path in self._agent_files()
        }
        registered = set(self._records)
        tracked: set[str] = set()
        stale_projects: list[dict[str, str]] = []

        for project in projects:
            if not isinstance(project, dict):
                continue
            agent_name = str(project.get("registered_agent") or "").strip()
            if project.get("registry_status") != "registered" or not agent_name:
                continue
            tracked.add(agent_name)
            if agent_name not in generated_files:
                stale_projects.append(
                    {
                        "project_id": str(project.get("project_id") or ""),
                        "agent": agent_name,
                    }
                )

        workspace = Path(workspace_agents) if workspace_agents is not None else None
        workspace_only = []
        if workspace is not None and workspace.is_dir():
            workspace_only = sorted(
                path.stem
                for path in workspace.glob("*.py")
                if path.is_file() and path.stem not in generated_files
            )

        return {
            "generated_dir": str(self.generated_dir),
            "registered_agents": sorted(registered),
            "invalid_generated_agents": sorted(set(generated_files) - registered),
            "orphan_generated_agents": sorted(registered - tracked),
            "stale_project_references": stale_projects,
            "workspace_only_agents": workspace_only,
        }

    def _agent_files(self) -> list[Path]:
        if not self.generated_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.generated_dir.glob("*.py")
            if path.is_file() and not path.name.startswith("_")
        )

    def _record_invalid(self, path: Path, error: str) -> None:
        self._invalid_records[path.stem] = {
            "agent_name": path.stem,
            "path": str(path),
            "status": "invalid",
            "checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
            "last_scanned_at": datetime.now(timezone.utc).isoformat(),
            "validation_ok": False,
            "error": error,
            "source": "dynamic_registry",
        }

    def spec_signature(self, spec: dict[str, Any] | None) -> str:
        if not spec:
            return ""
        return self._normalize_for_key(json.dumps(spec, sort_keys=True, ensure_ascii=False))

    def _normalize(self, value: str) -> str:
        text = unicodedata.normalize("NFKD", value.lower())
        text = "".join(char for char in text if unicodedata.category(char) != "Mn")
        cleaned = []
        for char in text:
            cleaned.append(char if char.isalnum() else " ")
        return " ".join("".join(cleaned).split())

    def _normalize_for_key(self, value: str) -> str:
        return self._normalize(value)

    def _match_text(
        self,
        module_name: str,
        agent_name: str,
        module_values: dict[str, Any],
        class_values: dict[str, Any],
        spec: dict[str, Any] | None,
    ) -> str:
        parts = [module_name, agent_name]
        if spec:
            parts.append(json.dumps(spec, sort_keys=True, ensure_ascii=False))

        for source in (module_values, class_values):
            for attr in ("name", "title", "goal", "target_event", "source", "description"):
                value = source.get(attr)
                if isinstance(value, (str, int, float, bool)):
                    parts.append(str(value))

        return self._normalize(" ".join(parts))
