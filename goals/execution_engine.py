from __future__ import annotations

import threading
import time
from typing import Any

from core.storage.sqlite_store import SQLiteStore


STEP_PROGRESS = {
    "queued": 0,
    "planning": 10,
    "code_generation": 35,
    "validation": 50,
    "compile": 60,
    "tests": 70,
    "business_validation": 75,
    "sandbox": 78,
    "runtime_governor": 80,
    "registry": 90,
    "verification": 95,
    "completed": 100,
}


class GoalExecutionEngine:
    def __init__(self, sqlite_store: SQLiteStore | None = None) -> None:
        self.sqlite_store = sqlite_store or SQLiteStore()

    def enqueue_goal(
        self,
        goal_id: str,
        title: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        run = {
            "goal_id": goal_id,
            "status": "queued",
            "current_step": "queued",
            "progress": 0,
            "plan_id": None,
            "project_id": None,
            "agent_slug": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "metadata": {
                "title": title,
                "source": source,
                **(metadata or {}),
            },
        }
        self.sqlite_store.create_goal_run(
            run,
            self._event(
                goal_id,
                "queued",
                "queued",
                "Goal accepted and queued",
                {"title": title, "source": source},
                now=now,
            ),
        )
        return self.get_goal_status(goal_id) or run

    def start_goal(self, goal_id: str) -> dict[str, Any] | None:
        current = self.sqlite_store.get_goal_run(goal_id)
        if current is None or current.get("status") == "running":
            return current
        now = time.time()
        return self.sqlite_store.update_goal_run(
            goal_id,
            {
                "status": "running",
                "current_step": "planning",
                "updated_at": now,
                "started_at": now,
                "finished_at": None,
                "error": None,
            },
            self._event(
                goal_id,
                "running",
                "running",
                "Goal execution started",
                now=now,
            ),
        )

    def mark_step_started(
        self,
        goal_id: str,
        step: str,
    ) -> dict[str, Any] | None:
        now = time.time()
        current = self.sqlite_store.get_goal_run(goal_id)
        progress = int((current or {}).get("progress") or 0)
        return self.sqlite_store.update_goal_run(
            goal_id,
            {
                "status": "running",
                "current_step": step,
                "progress": min(progress, STEP_PROGRESS.get(step, progress)),
                "updated_at": now,
            },
            self._event(
                goal_id,
                step,
                "started",
                f"Step {step} started",
                now=now,
            ),
        )

    def mark_step_completed(
        self,
        goal_id: str,
        step: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        payload = payload or {}
        current = self.sqlite_store.get_goal_run(goal_id) or {}
        progress = max(
            int(current.get("progress") or 0),
            STEP_PROGRESS.get(step, int(current.get("progress") or 0)),
        )
        updates: dict[str, Any] = {
            "status": "running",
            "current_step": step,
            "progress": progress,
            "updated_at": now,
        }
        for key in ("plan_id", "project_id", "agent_slug"):
            if payload.get(key):
                updates[key] = str(payload[key])
        return self.sqlite_store.update_goal_run(
            goal_id,
            updates,
            self._event(
                goal_id,
                step,
                "completed",
                f"Step {step} completed",
                payload,
                now=now,
            ),
        )

    def mark_step_failed(
        self,
        goal_id: str,
        step: str,
        error: str,
    ) -> dict[str, Any] | None:
        now = time.time()
        return self.sqlite_store.update_goal_run(
            goal_id,
            {
                "status": "failed",
                "current_step": step,
                "updated_at": now,
                "finished_at": now,
                "error": error,
            },
            self._event(
                goal_id,
                step,
                "failed",
                error,
                {"error": error},
                now=now,
            ),
        )

    def mark_sandbox_started(
        self,
        goal_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._mark_sandbox_event(
            goal_id,
            "sandbox_started",
            payload=payload,
        )

    def mark_sandbox_passed(
        self,
        goal_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._mark_sandbox_event(
            goal_id,
            "sandbox_passed",
            payload=payload,
            progress=STEP_PROGRESS["sandbox"],
        )

    def mark_sandbox_failed(
        self,
        goal_id: str,
        error: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._mark_sandbox_event(
            goal_id,
            "sandbox_failed",
            error=error,
            payload=payload,
        )

    def mark_sandbox_backend_event(
        self,
        goal_id: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        allowed = {
            "sandbox_backend_selected",
            "sandbox_systemd_started",
            "sandbox_systemd_failed",
            "sandbox_fallback_python",
        }
        if status not in allowed:
            raise ValueError(f"Unsupported sandbox event: {status}")
        return self._mark_sandbox_event(
            goal_id,
            status,
            payload=payload,
        )

    def complete_goal(self, goal_id: str) -> dict[str, Any] | None:
        now = time.time()
        return self.sqlite_store.update_goal_run(
            goal_id,
            {
                "status": "completed",
                "current_step": "completed",
                "progress": 100,
                "updated_at": now,
                "finished_at": now,
                "error": None,
            },
            self._event(
                goal_id,
                "completed",
                "completed",
                "Goal execution completed",
                now=now,
            ),
        )

    def fail_goal(self, goal_id: str, error: str) -> dict[str, Any] | None:
        return self.mark_step_failed(goal_id, "failed", error)

    def get_goal_status(self, goal_id: str) -> dict[str, Any] | None:
        return self.sqlite_store.get_goal_run(goal_id)

    def list_goals(self) -> list[dict[str, Any]]:
        return self.sqlite_store.list_goal_runs()

    def get_goal_events(self, goal_id: str) -> list[dict[str, Any]]:
        return self.sqlite_store.list_goal_events(goal_id)

    def recover_interrupted_goals(self) -> list[str]:
        return self.sqlite_store.interrupt_running_goal_runs(time.time())

    def _mark_sandbox_event(
        self,
        goal_id: str,
        event_status: str,
        *,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
        progress: int | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        current = self.sqlite_store.get_goal_run(goal_id) or {}
        updates: dict[str, Any] = {
            "current_step": "sandbox",
            "updated_at": now,
        }
        if progress is not None:
            updates["progress"] = max(
                int(current.get("progress") or 0),
                progress,
            )
        if event_status == "sandbox_failed":
            updates.update(
                {
                    "status": "failed",
                    "finished_at": now,
                    "error": error,
                }
            )
        else:
            updates["status"] = "running"
        event_payload = dict(payload or {})
        if error:
            event_payload["error"] = error
        return self.sqlite_store.update_goal_run(
            goal_id,
            updates,
            self._event(
                goal_id,
                "sandbox",
                event_status,
                event_status,
                event_payload,
                now=now,
            ),
        )

    def _event(
        self,
        goal_id: str,
        step: str,
        status: str,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        return {
            "goal_id": goal_id,
            "step": step,
            "status": status,
            "message": message,
            "payload": payload or {},
            "created_at": now or time.time(),
        }


_ENGINE: GoalExecutionEngine | None = None
_ENGINE_LOCK = threading.Lock()


def get_goal_execution_engine() -> GoalExecutionEngine:
    from goal.goals.goal_manager import get_goal_manager

    global _ENGINE
    store = get_goal_manager().sqlite_store
    with _ENGINE_LOCK:
        if _ENGINE is None or _ENGINE.sqlite_store.path != store.path:
            _ENGINE = GoalExecutionEngine(store)
        return _ENGINE
