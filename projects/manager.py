from __future__ import annotations

import json
import time
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from modules.events.event import Event
from modules.events.event_bus import event_bus
from modules.events.event_types import PROJECT_CREATED
from core.storage.sqlite_store import SQLiteStore, get_path_lock


PROJECTS_PATH = Path("/etc/neron/data/projects.json")


def _now() -> float:
    return time.time()


class ProjectManager:
    def __init__(
        self,
        path: Path = PROJECTS_PATH,
        evolution_state_path: Path | None = None,
        sqlite_store: SQLiteStore | None = None,
    ) -> None:
        self.path = path
        self.evolution_state_path = evolution_state_path or path.parent / "evolution_state.json"
        self.sqlite_store = sqlite_store or SQLiteStore(path.parent / "neron_state.sqlite3")
        self._lock = get_path_lock(path)
        self._legacy_imported = False

    def create_project(
        self,
        *,
        title: str,
        project_type: str,
        requested_by: str = "user",
        source_channel: str = "api",
        query: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            projects = self._load()
            project_id = self._make_project_id(title, project_type)
            created_at = _now()
            project = {
                "project_id": project_id,
                "title": title,
                "type": project_type,
                "status": "pending",
                "current_step": "created",
                "progress": 0,
                "requested_by": requested_by,
                "source_channel": source_channel,
                "query": query,
                "created_at": created_at,
                "updated_at": created_at,
                "steps": [],
                "created_files": [],
                "test_results": [],
                "validation_status": "pending",
                "compile_status": "pending",
                "test_status": "pending",
                "business_validation_status": "pending",
                "business_validation_result": None,
                "sandbox_status": "pending",
                "sandbox_result": None,
                "sandbox_backend": None,
                "sandbox_isolation_level": None,
                "governor_status": "pending",
                "registry_status": "not_registered",
                "runtime_status": "pending",
                "result": None,
                "error": None,
                "metadata": metadata or {},
            }
            projects.append(project)
            self._save(projects)
            event_bus.publish_background(Event(
                type=PROJECT_CREATED,
                source="project_manager",
                payload={
                    "project_id": project_id,
                    "title": title,
                    "project_type": project_type,
                    "goal_id": project["metadata"].get("goal_id"),
                    "plan_id": project["metadata"].get("plan_id"),
                },
            ))
            return project

    def update_project(
        self,
        project_id: str,
        updates: dict[str, Any] | None = None,
        *,
        step: str | None = None,
        step_status: str | None = None,
        progress: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            projects = self._load()
            for project in projects:
                if project.get("project_id") != project_id:
                    continue

                if step:
                    project["current_step"] = step
                    project.setdefault("steps", []).append(
                        {
                            "name": step,
                            "status": step_status or "done",
                            "at": _now(),
                            "error": error,
                        }
                    )
                if updates:
                    project.update(updates)
                if progress is not None:
                    project["progress"] = max(0, min(100, int(progress)))
                if error:
                    project["error"] = error
                if project.get("status") == "completed":
                    project["current_step"] = "completed"
                project["updated_at"] = _now()
                self._save(projects)
                return project
        return None

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        for project in self._load():
            if project.get("project_id") == project_id:
                return project
        return None

    def find_project_by_tracking(
        self,
        *,
        goal_id: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any] | None:
        tracked = self.sqlite_store.find_project_by_tracking(
            goal_id=goal_id,
            plan_id=plan_id,
        )
        if tracked is not None:
            return tracked
        for project in reversed(self._load()):
            metadata = project.get("metadata") or {}
            if goal_id and str(metadata.get("goal_id") or "") == goal_id:
                return project
            if plan_id and str(metadata.get("plan_id") or "") == plan_id:
                return project
        return None

    def list_projects(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        projects = self._load()
        if status:
            projects = [project for project in projects if project.get("status") == status]
        return [
            self._with_business_validation_status(project)
            for project in reversed(projects[-max(1, min(limit, 500)):])
        ]

    def diagnose_recent_failures(self, limit: int = 10) -> dict[str, Any]:
        failed_projects = [
            project for project in self._load()
            if project.get("status") == "failed"
        ]
        failed_projects.sort(key=lambda project: float(project.get("updated_at") or project.get("created_at") or 0))
        selected = list(reversed(failed_projects[-max(1, min(limit, 100)):]))
        evolution_runs = self._load_evolution_runs_by_project()
        diagnostics = [
            self._diagnose_failed_project(
                project,
                evolution_runs.get(str(project.get("project_id") or "")),
            )
            for project in selected
        ]
        categories = Counter(item["category"] for item in diagnostics)

        return {
            "generated_at": _now(),
            "count": len(diagnostics),
            "categories": [
                {"category": category, "count": count}
                for category, count in categories.most_common()
            ],
            "projects": diagnostics,
            "summary": self._format_failure_summary(diagnostics, categories),
        }

    def format_failure_report(self, limit: int = 10) -> str:
        report = self.diagnose_recent_failures(limit=limit)
        lines = [
            "Diagnostic projets failed récents",
            f"Total : {report['count']}",
        ]
        if not report["projects"]:
            lines.append("Aucun projet failed trouvé.")
            return "\n".join(lines)

        lines.append("Catégories :")
        for category in report["categories"]:
            lines.append(f"- {category['category']} : {category['count']}")
        lines.append("Projets :")
        for project in report["projects"]:
            lines.append(
                "- "
                f"{project['project_id']} | "
                f"{project['category']} | "
                f"{project['probable_cause']}"
            )
        lines.append("Relance automatique : non, validation humaine requise.")
        return "\n".join(lines)

    def find_project_by_query(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        normalized = self._normalize(query)
        keywords = [word for word in normalized.split() if len(word) >= 3]
        if not keywords:
            return []

        matches = []
        for project in reversed(self._load()):
            haystack = self._normalize(
                " ".join(
                    str(project.get(field) or "")
                    for field in ("project_id", "title", "type", "query")
                )
            )
            metadata = project.get("metadata") or {}
            haystack += " " + self._normalize(json.dumps(metadata, ensure_ascii=False))
            if all(keyword in haystack for keyword in keywords[:3]) or any(
                keyword in haystack for keyword in keywords
            ):
                matches.append(project)
            if len(matches) >= limit:
                break
        return matches

    def find_existing_agent_project(
        self,
        *,
        agent_name: str,
        intent_key: str,
        spec_signature: str,
        statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        allowed_statuses = statuses or {"completed", "running"}
        normalized_agent = self._normalize(agent_name)
        normalized_intent = self._normalize(intent_key)
        normalized_spec = self._normalize(spec_signature)
        requested_years = self._explicit_years(normalized_spec)

        for project in reversed(self._load()):
            if project.get("status") not in allowed_statuses:
                continue

            metadata = project.get("metadata") or {}
            spec = metadata.get("spec") or {}
            project_intent = self._normalize(str(metadata.get("intent_key") or ""))
            project_signature = self._normalize(str(metadata.get("spec_signature") or ""))

            if project_intent or project_signature:
                if normalized_intent and project_intent == normalized_intent:
                    return project
                if normalized_spec and project_signature == normalized_spec:
                    return project
                continue

            candidates = [
                project.get("registered_agent"),
                metadata.get("agent_name"),
                spec.get("name") if isinstance(spec, dict) else None,
            ]
            project_identity = self._normalize(
                " ".join(
                    [
                        str(project.get("title") or ""),
                        str(project.get("query") or ""),
                        json.dumps(spec, ensure_ascii=False) if isinstance(spec, dict) else "",
                    ]
                )
            )
            if requested_years != self._explicit_years(project_identity):
                continue
            if normalized_agent and any(
                self._normalize(str(item or "")) == normalized_agent
                for item in candidates
            ):
                return project

        return None

    def _load(self) -> list[dict[str, Any]]:
        if not self._legacy_imported:
            for project in self._load_legacy():
                project_id = str(project.get("project_id") or "")
                if project_id and self.sqlite_store.get_project(project_id) is None:
                    self.sqlite_store.upsert_project(project)
            self._legacy_imported = True
        return self.sqlite_store.list_projects()

    def _load_legacy(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        projects = data.get("projects", []) if isinstance(data, dict) else []
        return projects if isinstance(projects, list) else []

    def _save(self, projects: list[dict[str, Any]]) -> None:
        selected = projects[-500:]
        for project in selected:
            self.sqlite_store.upsert_project(project)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp.write_text(
            json.dumps({"projects": selected}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def _make_project_id(self, title: str, project_type: str) -> str:
        words = [
            word
            for word in self._normalize(title).split()
            if word not in {"creer", "agent", "tool", "outil", "pour", "avec", "qui", "me", "donne"}
        ]
        base = "_".join(words[:4]) or project_type
        return f"{project_type}_{base}_{uuid.uuid4().hex[:8]}"

    def _normalize(self, value: str) -> str:
        text = unicodedata.normalize("NFKD", value.lower())
        text = "".join(char for char in text if unicodedata.category(char) != "Mn")
        cleaned = []
        for char in text:
            cleaned.append(char if char.isalnum() else " ")
        return " ".join("".join(cleaned).split())

    def _explicit_years(self, value: str) -> set[str]:
        return {
            token
            for token in self._normalize(value).split()
            if len(token) == 4 and token.isdigit() and 1900 <= int(token) <= 2199
        }

    def _with_business_validation_status(
        self,
        project: dict[str, Any],
    ) -> dict[str, Any]:
        exposed = dict(project)
        if not exposed.get("business_validation_status"):
            exposed["business_validation_status"] = (
                "not_validated"
                if exposed.get("status") == "completed"
                else "pending"
            )
        exposed.setdefault("business_validation_result", None)
        return exposed

    def _diagnose_failed_project(
        self,
        project: dict[str, Any],
        related_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        category, cause, evidence = self._failure_category(project, related_run)
        last_failed_step = self._last_step_with_status(project, "failed")
        last_completed_step = self._last_completed_step(project)
        return {
            "project_id": project.get("project_id"),
            "related_run_id": related_run.get("run_id") if related_run else None,
            "title": project.get("title"),
            "type": project.get("type"),
            "current_step": project.get("current_step"),
            "last_failed_step": last_failed_step.get("name") if last_failed_step else None,
            "last_completed_step": last_completed_step.get("name") if last_completed_step else None,
            "progress": project.get("progress"),
            "updated_at": project.get("updated_at"),
            "error": project.get("error"),
            "category": category,
            "probable_cause": cause,
            "evidence": evidence,
            "retry_requires_validation": True,
        }

    def _failure_category(
        self,
        project: dict[str, Any],
        related_run: dict[str, Any] | None = None,
    ) -> tuple[str, str, list[str]]:
        text = self._failure_text(project, related_run)
        current_step = str(project.get("current_step") or "")
        last_failed_step = self._last_step_with_status(project, "failed")
        failed_step_name = str((last_failed_step or {}).get("name") or current_step)
        last_completed_step = self._last_completed_step(project)
        last_completed_name = str((last_completed_step or {}).get("name") or "")
        evidence = self._failure_evidence(project, last_failed_step, last_completed_step, related_run)

        if self._contains_any(text, ("codex cli introuvable", "no such file or directory codex")):
            return (
                "codex_cli_unavailable",
                "Codex n'était pas disponible dans l'environnement d'exécution.",
                evidence,
            )
        if self._contains_any(text, ("unexpected argument", "ask for approval", "approval mode", "approval policy")):
            return (
                "codex_cli_arguments",
                "La commande Codex a échoué à cause d'une option CLI incompatible.",
                evidence,
            )
        if (
            self._contains_any(text, ("codex a echoue", "codex failed"))
            or failed_step_name == "codex_running"
            or (failed_step_name == "failed" and last_completed_name == "codex_running")
        ):
            return (
                "codex_execution_failed",
                "Codex a échoué avant la phase de tests; consulter stderr/stdout du run d'évolution.",
                evidence,
            )
        if self._contains_any(text, ("tests en echec", "pytest", "test failed")) or failed_step_name == "tests":
            return (
                "tests_failed",
                "La validation automatisée a échoué.",
                evidence,
            )
        if self._contains_any(text, ("py_compile", "compile", "syntax_error")) or failed_step_name == "compile":
            return (
                "compile_failed",
                "Le code généré ne compile pas.",
                evidence,
            )
        if (
            self._contains_any(text, ("validation", "class agent manquante", "execute manquante"))
            or failed_step_name == "validation"
        ):
            return (
                "validation_failed",
                "La structure de l'agent généré ne respecte pas le contrat attendu.",
                evidence,
            )
        if self._contains_any(text, ("verification", "runtime")) or failed_step_name == "verification":
            return (
                "runtime_verification_failed",
                "L'agent existe mais son appel de vérification runtime a échoué.",
                evidence,
            )
        if self._contains_any(text, ("commit", "push")) or failed_step_name == "commit":
            return (
                "commit_failed",
                "La phase de commit ou push a échoué après les changements.",
                evidence,
            )
        return (
            "unknown_failed",
            "Les données stockées ne suffisent pas à isoler une cause précise.",
            evidence,
        )

    def _failure_text(
        self,
        project: dict[str, Any],
        related_run: dict[str, Any] | None = None,
    ) -> str:
        parts = [
            str(project.get("current_step") or ""),
            str(project.get("error") or ""),
        ]
        metadata = project.get("metadata") or {}
        if isinstance(metadata, dict):
            for key in ("failure_context", "codex", "commit", "tests"):
                if metadata.get(key):
                    parts.append(json.dumps(metadata.get(key), ensure_ascii=False))
        for step in project.get("steps") or []:
            if isinstance(step, dict):
                parts.extend(
                    [
                        str(step.get("name") or ""),
                        str(step.get("status") or ""),
                        str(step.get("error") or ""),
                    ]
                )
        for result in project.get("test_results") or []:
            if isinstance(result, dict):
                parts.extend(
                    [
                        str(result.get("name") or ""),
                        str(result.get("stdout_tail") or result.get("stdout") or ""),
                        str(result.get("stderr_tail") or result.get("stderr") or ""),
                    ]
                )
        if related_run:
            parts.append(str(related_run.get("error") or ""))
            for key in ("codex", "commit", "tests"):
                if related_run.get(key):
                    parts.append(self._diagnostic_payload_text(related_run.get(key)))
        return self._normalize(" ".join(parts))

    def _failure_evidence(
        self,
        project: dict[str, Any],
        last_failed_step: dict[str, Any] | None,
        last_completed_step: dict[str, Any] | None,
        related_run: dict[str, Any] | None = None,
    ) -> list[str]:
        evidence = [
            f"status={project.get('status')}",
            f"current_step={project.get('current_step')}",
            f"progress={project.get('progress')}",
        ]
        if related_run:
            evidence.append(f"related_run_id={related_run.get('run_id')}")
            evidence.append(f"run_step={related_run.get('current_step')}")
            if related_run.get("error"):
                evidence.append(f"run_error={related_run.get('error')}")
            codex = related_run.get("codex")
            if isinstance(codex, dict):
                evidence.append(f"codex_returncode={codex.get('returncode')}")
                evidence.append(f"codex_timed_out={bool(codex.get('timed_out'))}")
                stderr_tail = self._tail_text(str(codex.get("stderr") or ""))
                stdout_tail = self._tail_text(str(codex.get("stdout") or ""))
                if stderr_tail:
                    evidence.append(f"codex_stderr_tail={stderr_tail}")
                if stdout_tail:
                    evidence.append(f"codex_stdout_tail={stdout_tail}")
        if last_completed_step:
            evidence.append(f"last_completed_step={last_completed_step.get('name')}")
        if last_failed_step:
            evidence.append(f"last_failed_step={last_failed_step.get('name')}")
            if last_failed_step.get("error"):
                evidence.append(f"step_error={last_failed_step.get('error')}")
        if project.get("error"):
            evidence.append(f"error={project.get('error')}")
        if project.get("test_results"):
            evidence.append(f"test_results={len(project.get('test_results') or [])}")
        return evidence

    def _load_evolution_runs_by_project(self) -> dict[str, dict[str, Any]]:
        if not self.evolution_state_path.exists():
            return {}
        try:
            data = json.loads(self.evolution_state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        runs = data.get("runs", []) if isinstance(data, dict) else []
        if not isinstance(runs, list):
            return {}

        by_project: dict[str, dict[str, Any]] = {}
        for run in runs:
            if not isinstance(run, dict):
                continue
            project_id = str(run.get("project_id") or "")
            if not project_id:
                continue
            current = by_project.get(project_id)
            if not current or self._run_sort_key(run) >= self._run_sort_key(current):
                by_project[project_id] = run
        return by_project

    def _run_sort_key(self, run: dict[str, Any]) -> str:
        return str(run.get("updated_at") or run.get("created_at") or "")

    def _diagnostic_payload_text(self, payload: Any) -> str:
        if isinstance(payload, list):
            return " ".join(self._diagnostic_payload_text(item) for item in payload)
        if not isinstance(payload, dict):
            return str(payload)

        parts = [
            str(payload.get("name") or ""),
            str(payload.get("returncode") or ""),
            str(payload.get("timed_out") or ""),
            str(payload.get("error") or ""),
            str(payload.get("stderr") or payload.get("stderr_tail") or ""),
            str(payload.get("stdout") or payload.get("stdout_tail") or ""),
        ]
        for key in ("add", "commit", "push", "status"):
            if isinstance(payload.get(key), dict):
                parts.append(self._diagnostic_payload_text(payload[key]))
        return " ".join(parts)

    def _tail_text(self, value: str, limit: int = 240) -> str:
        text = " ".join(value.split())
        if len(text) <= limit:
            return text
        return text[-limit:]

    def _last_step_with_status(self, project: dict[str, Any], status: str) -> dict[str, Any] | None:
        for step in reversed(project.get("steps") or []):
            if isinstance(step, dict) and step.get("status") == status:
                return step
        return None

    def _last_completed_step(self, project: dict[str, Any]) -> dict[str, Any] | None:
        for step in reversed(project.get("steps") or []):
            if isinstance(step, dict) and step.get("status") == "done":
                return step
        return None

    def _contains_any(self, text: str, needles: tuple[str, ...]) -> bool:
        return any(self._normalize(needle) in text for needle in needles)

    def _format_failure_summary(
        self,
        diagnostics: list[dict[str, Any]],
        categories: Counter[str],
    ) -> str:
        if not diagnostics:
            return "Aucun projet failed récent à diagnostiquer."
        category_text = ", ".join(
            f"{category}={count}" for category, count in categories.most_common()
        )
        return (
            f"{len(diagnostics)} projet(s) failed analysé(s). "
            f"Catégories probables: {category_text}. "
            "Aucune relance automatique n'est déclenchée."
        )


_project_manager: ProjectManager | None = None


def get_project_manager() -> ProjectManager:
    global _project_manager
    if _project_manager is None:
        _project_manager = ProjectManager()
    return _project_manager
