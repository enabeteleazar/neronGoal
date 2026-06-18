from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

from goal.goals.goal_manager import get_goal_manager


logger = logging.getLogger("neron.goals.background")


@dataclass
class _ThreadResult:
    error: Exception | None = None
    value: dict | None = None


class GoalBackgroundRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = threading.RLock()

    def submit(self, *, goal_id: str, objective: str, source: str) -> None:
        with self._lock:
            current = self._tasks.get(goal_id)
            if current is not None and not current.done():
                return
            task = asyncio.create_task(
                self._execute(goal_id=goal_id, objective=objective, source=source),
                name=f"goal-workflow-{goal_id}",
            )
            self._tasks[goal_id] = task
            task.add_done_callback(
                lambda completed, tracked_goal_id=goal_id: self._discard(
                    tracked_goal_id,
                    completed,
                )
            )

    async def wait(self, goal_id: str, timeout: float | None = None) -> None:
        with self._lock:
            task = self._tasks.get(goal_id)
        if task is None:
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def shutdown(self) -> None:
        with self._lock:
            tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def active_goal_ids(self) -> list[str]:
        with self._lock:
            return sorted(
                goal_id
                for goal_id, task in self._tasks.items()
                if not task.done()
            )

    async def _execute(self, *, goal_id: str, objective: str, source: str) -> None:
        from goal.goals.execution_engine import get_goal_execution_engine

        execution_engine = get_goal_execution_engine()
        execution_engine.start_goal(goal_id)
        result = _ThreadResult()
        thread = threading.Thread(
            target=self._run_thread,
            args=(result, goal_id, objective, source),
            name=f"goal-worker-{goal_id}",
            daemon=True,
        )
        thread.start()
        try:
            while thread.is_alive():
                await asyncio.sleep(0.05)
            if result.error is not None:
                raise result.error
            final_status = str((result.value or {}).get("status") or "")
            current = execution_engine.get_goal_status(goal_id) or {}
            if current.get("status") not in {"completed", "failed"}:
                if final_status in {"completed", "plan_finished"}:
                    execution_engine.complete_goal(goal_id)
                elif final_status in {"failed", "refused", "blocked_by_risk"}:
                    execution_engine.fail_goal(
                        goal_id,
                        str((result.value or {}).get("error") or final_status),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "background_goal_failed goal_id=%s error=%s",
                goal_id,
                exc,
            )
            execution_engine.fail_goal(goal_id, str(exc))
            get_goal_manager().update_status(
                goal_id,
                "failed",
                current_step="failed",
                error=str(exc),
            )

    def _run_thread(
        self,
        result: _ThreadResult,
        goal_id: str,
        objective: str,
        source: str,
    ) -> None:
        try:
            result.value = self._run_workflow(goal_id, objective, source)
        except Exception as exc:
            result.error = exc

    def _run_workflow(
        self,
        goal_id: str,
        objective: str,
        source: str,
    ) -> dict:
        from goal.goals.goal_orchestrator import get_goal_orchestrator

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(
                get_goal_orchestrator().run_goal(
                    objective,
                    source=source,
                    goal_id=goal_id,
                )
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def _discard(self, goal_id: str, task: asyncio.Task[None]) -> None:
        with self._lock:
            if self._tasks.get(goal_id) is task:
                self._tasks.pop(goal_id, None)


_runner: GoalBackgroundRunner | None = None
_runner_lock = threading.Lock()


def get_goal_background_runner() -> GoalBackgroundRunner:
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = GoalBackgroundRunner()
        return _runner
