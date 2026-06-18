from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modules.events.event import Event
from modules.events.event_bus import event_bus
from modules.events.event_types import PLAN_CREATED
from core.storage.sqlite_store import SQLiteStore, get_path_lock


DEFAULT_PLAN_HISTORY_PATH = Path("/etc/neron/data/plans.jsonl")


class PlanStorage:
    def __init__(
        self,
        path: Path = DEFAULT_PLAN_HISTORY_PATH,
        sqlite_store: SQLiteStore | None = None,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite_store = sqlite_store or SQLiteStore(
            path.parent / "neron_state.sqlite3"
        )
        self._lock = get_path_lock(path)
        self._legacy_imported = False

    def save(self, plan: dict[str, Any]) -> None:
        with self._lock:
            is_new = self.sqlite_store.get_workflow(str(plan.get("id") or "")) is None
            self.sqlite_store.upsert_workflow(plan)
            plans = self._read_all()
            if not any(item.get("id") == plan.get("id") for item in plans):
                plans.append(plan)
            self._write_legacy(plans)
            if is_new:
                event_bus.publish_background(Event(
                    type=PLAN_CREATED,
                    source="plan_storage",
                    payload={
                        "plan_id": plan.get("id"),
                        "goal_id": plan.get("goal_id"),
                        "goal": plan.get("goal"),
                        "step_count": len(plan.get("steps") or []),
                    },
                ))

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        plans = self._read_all()
        return list(reversed(plans[-limit:]))

    def last(self) -> dict[str, Any] | None:
        plans = self._read_all()
        return plans[-1] if plans else None

    def get(self, plan_id: str) -> dict[str, Any] | None:
        for plan in reversed(self._read_all()):
            if plan.get("id") == plan_id:
                return plan
        return None

    def find_by_goal_id(self, goal_id: str) -> dict[str, Any] | None:
        stored = self.sqlite_store.find_workflow_by_goal(goal_id)
        if stored is not None:
            return self._normalize_plan(stored)
        for plan in reversed(self._read_all()):
            if str(plan.get("goal_id") or "") == goal_id:
                return plan
        return None

    def update(self, updated_plan: dict[str, Any]) -> None:
        with self._lock:
            self.sqlite_store.upsert_workflow(updated_plan)
            plans = self._read_all()
            plan_id = updated_plan.get("id")
            written = False
            updated: list[dict[str, Any]] = []
            for plan in plans:
                if plan.get("id") == plan_id:
                    updated.append(updated_plan)
                    written = True
                else:
                    updated.append(plan)
            if not written:
                updated.append(updated_plan)
            self._write_legacy(updated)

    def _read_all(self) -> list[dict[str, Any]]:
        if not self._legacy_imported:
            for plan in self._read_legacy():
                plan_id = str(plan.get("id") or "")
                if plan_id and self.sqlite_store.get_workflow(plan_id) is None:
                    self.sqlite_store.upsert_workflow(plan)
            self._legacy_imported = True
        return [
            self._normalize_plan(plan)
            for plan in self.sqlite_store.list_workflows()
        ]

    def _read_legacy(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        plans = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                plan = json.loads(line)
            except json.JSONDecodeError:
                continue

            plans.append(self._normalize_plan(plan))

        return plans

    def _normalize_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        if plan.get("status") == "done":
            plan["status"] = "plan_finished"
        for step in plan.get("steps", []):
            if step.get("status") == "done":
                step["status"] = "completed"
        return plan

    def _write_legacy(self, plans: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for plan in plans:
                handle.write(json.dumps(plan, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
