from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules.events.event import Event
from modules.events.event_bus import event_bus
from modules.events.event_types import TASK_CREATED
from core.storage.sqlite_store import SQLiteStore, get_path_lock


TASKS_FILE = Path("/etc/neron/data/tasks.json")


def normalize_task_title(title: str | None) -> str:
    if not title:
        return ""

    value = title.lower().strip()

    replacements = {
        "analyser": "surveiller",
        "contrôler": "vérifier",
        "controler": "vérifier",
        "etat": "état",
        "ressources systeme": "ressources système",
        "boucle cognitive": "boucle cognitive",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return value


class TaskManager:
    def __init__(
        self,
        path: Path | str | None = None,
        sqlite_store: SQLiteStore | None = None,
    ) -> None:
        self.path = Path(path or TASKS_FILE)
        self.sqlite_store = sqlite_store or SQLiteStore(
            self.path.parent / "neron_state.sqlite3"
        )
        self._lock = get_path_lock(self.path)
        self.tasks: list[dict[str, Any]] = []
        self._legacy_imported = False
        self._load()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _load(self) -> None:
        with self._lock:
            stored = self.sqlite_store.list_workflow_tasks()
            if not stored and not self._legacy_imported:
                for task in self._load_legacy():
                    if task.get("id"):
                        self.sqlite_store.upsert_workflow_task(task)
                self._legacy_imported = True
                stored = self.sqlite_store.list_workflow_tasks()
            self.tasks = stored

    def _load_legacy(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict):
            tasks = data.get("tasks", [])
        elif isinstance(data, list):
            tasks = data
        else:
            tasks = []
        return [task for task in tasks if isinstance(task, dict)]

    def save(self) -> None:
        with self._lock:
            for task in self.tasks:
                self.sqlite_store.upsert_workflow_task(task)
            self.tasks = self.sqlite_store.list_workflow_tasks()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
            tmp.write_text(
                json.dumps(
                    {"tasks": self.tasks},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            tmp.replace(self.path)

    def create_task(
        self,
        title: str,
        description: str | None = None,
        priority: str = "medium",
        status: str = "active",
        source: str = "manual",
        metadata: dict[str, Any] | None = None,
        goal_id: str | None = None,
        plan_id: str | None = None,
        goal: str | None = None,
        agent: str | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._load()
            task = {
                "id": str(uuid.uuid4()),
                "title": title,
                "description": description,
                "priority": priority,
                "status": status,
                "source": source,
                "metadata": metadata or {},
                "progress": 0,
                "created_at": self._now(),
                "updated_at": self._now(),
                "completed_at": None,
            }
            if goal_id:
                task["goal_id"] = goal_id
            if plan_id:
                task["plan_id"] = plan_id
            if goal:
                task["goal"] = goal
            if agent:
                task["agent"] = agent
            if action:
                task["action"] = action

            self.tasks.append(task)
            self.save()
            persisted = self.get_task(str(task["id"])) or task
            event_bus.publish_background(Event(
                type=TASK_CREATED,
                source="task_manager",
                payload={
                    "task_id": persisted["id"],
                    "title": persisted["title"],
                    "source": persisted["source"],
                    "goal_id": goal_id or persisted["metadata"].get("goal_id"),
                    "plan_id": plan_id or persisted["metadata"].get("plan_id"),
                    "agent": agent,
                    "action": action,
                },
            ))
            return persisted

    def list_tasks(
        self,
        status: str | None = None,
        priority: str | None = None,
    ) -> list[dict[str, Any]]:
        tasks = self.tasks
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        if priority:
            tasks = [task for task in tasks if task.get("priority") == priority]
        return tasks

    def list_active_tasks(self) -> list[dict[str, Any]]:
        return [
            task
            for task in self.tasks
            if task.get("status") in {"pending", "active", "todo", "in_progress"}
        ]

    def get_active_tasks(self) -> list[dict[str, Any]]:
        return self.list_active_tasks()

    def list_running_tasks(self) -> list[dict[str, Any]]:
        return [task for task in self.tasks if task.get("status") in {"running", "in_progress"}]

    def get_next_task(self) -> dict[str, Any] | None:
        return self.next_active_task()

    def start_next_task(self) -> dict[str, Any] | None:
        task = self.next_active_task()
        if not task:
            return None
        return self.update_status(str(task.get("id")), "running")

    def get_status_summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        for task in self.tasks:
            status = str(task.get("status") or "unknown")
            priority = str(task.get("priority") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
            by_priority[priority] = by_priority.get(priority, 0) + 1

        return {
            "total": len(self.tasks),
            "active": len(self.list_active_tasks()),
            "running": len(self.list_running_tasks()),
            "by_status": by_status,
            "by_priority": by_priority,
        }

    def ensure_default_tasks_for_goal(self, goal: str | None) -> None:
        if not goal:
            return

        if self.list_active_tasks():
            return

        normalized_goal = goal.lower()

        if "stabilité" in normalized_goal or "stabilite" in normalized_goal:
            self.create_task(
                title="Surveiller neron-core",
                description="Vérifier que le service principal reste actif.",
                priority="high",
                source="goal_system",
            )

            self.create_task(
                title="Surveiller les ressources système",
                description="Observer CPU, RAM et disque via SelfModel.",
                priority="high",
                source="goal_system",
            )

            self.create_task(
                title="Vérifier la boucle cognitive",
                description="Confirmer que la boucle cognitive reste active.",
                priority="medium",
                source="goal_system",
            )


    def create_tasks_from_plan(
        self,
        plan: dict[str, Any],
    ) -> list[dict[str, Any]]:
        with self._lock:
            goal = str(plan.get("goal") or "")
            goal_id = str(plan.get("goal_id") or "")
            plan_id = str(plan.get("id") or "")
            created: list[dict[str, Any]] = []

            existing_keys = {
                (
                    normalize_task_title(task.get("title")),
                    task.get("source"),
                    task.get("plan_id"),
                )
                for task in self.tasks
            }

            for step in plan.get("steps", []):
                title = str(step.get("title") or "").strip()
                if not title:
                    continue

                key = (
                    normalize_task_title(title),
                    "planner",
                    plan_id,
                )

                if key in existing_keys:
                    continue

                task = self.create_task(
                    title=title,
                    description=str(step.get("description") or goal),
                    priority="medium",
                    status="active",
                    source="planner",
                    metadata={
                        "goal_id": goal_id or None,
                        "plan_id": plan_id or None,
                    },
                    goal_id=goal_id or None,
                    plan_id=plan_id or None,
                    goal=goal or None,
                    agent=step.get("agent"),
                    action=step.get("action"),
                )

                created.append(task)
                existing_keys.add(key)

            return created


    def get_task(self, task_id: str) -> dict[str, Any] | None:
        for task in self.tasks:
            if task.get("id") == task_id:
                return task
        return None

    def next_active_task(self) -> dict[str, Any] | None:
        active_statuses = {"pending", "active", "todo", "in_progress"}

        for task in self.tasks:
            if (
                task.get("status") in active_statuses
                and task.get("source") == "planner"
                and task.get("action")
            ):
                return task

        for task in self.tasks:
            if (
                task.get("status") in active_statuses
                and task.get("action")
            ):
                return task

        for task in self.tasks:
            if task.get("status") in active_statuses:
                return task

        return None

    def update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._lock:
            self._load()
            task = self.get_task(task_id)

            if not task:
                return None

            task.update(updates)
            task["updated_at"] = self._now()
            self.save()
            return self.get_task(task_id)

    def update_status(self, task_id: str, status: str) -> dict[str, Any] | None:
        allowed_statuses = {
            "pending",
            "active",
            "todo",
            "in_progress",
            "running",
            "done",
            "completed",
            "failed",
            "cancelled",
        }
        if status not in allowed_statuses:
            raise ValueError(f"Invalid task status: {status}")

        updates: dict[str, Any] = {"status": status}
        if status in {"done", "completed"}:
            updates["progress"] = 100
            updates["completed_at"] = self._now()
        elif status in {"running", "in_progress"}:
            updates["progress"] = max(1, int(self.get_task(task_id).get("progress") or 0)) if self.get_task(task_id) else 1

        return self.update_task(task_id, updates)

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self.tasks)
            self.tasks = [task for task in self.tasks if task.get("id") != task_id]
            deleted = len(self.tasks) != before
            if deleted:
                self.sqlite_store.delete_workflow_task(task_id)
                self.save()
            return deleted

    def clear_done(self) -> int:
        with self._lock:
            self._load()
            done_statuses = {"done", "completed"}
            before = len(self.tasks)
            removed_ids = [
                str(task.get("id"))
                for task in self.tasks
                if task.get("status") in done_statuses and task.get("id")
            ]
            self.tasks = [task for task in self.tasks if task.get("status") not in done_statuses]
            removed = before - len(self.tasks)
            if removed:
                for task_id in removed_ids:
                    self.sqlite_store.delete_workflow_task(task_id)
                self.save()
            return removed

    def fail_task(
        self,
        task_id: str,
        error: str,
    ) -> dict[str, Any] | None:
        return self.update_task(
            task_id,
            {
                "status": "failed",
                "error": error,
                "progress": task.get("progress", 0) if (task := self.get_task(task_id)) else 0,
            },
        )

    def complete_task(self, task_id: str) -> bool:
        with self._lock:
            self._load()
            for task in self.tasks:
                if task.get("id") == task_id:
                    task["status"] = "completed"
                    task["progress"] = 100
                    task["updated_at"] = self._now()
                    task["completed_at"] = self._now()
                    self.save()
                    return True

        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "active_tasks": self.list_active_tasks(),
        }


_task_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    global _task_manager

    if _task_manager is None:
        _task_manager = TaskManager()

    return _task_manager
