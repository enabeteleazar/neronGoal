from __future__ import annotations

# Rapatrié depuis server/core/storage/sqlite_store.py (phase 2).
# Base de données PROPRE au service goal : plus aucun partage de fichier
# SQLite avec le core. Migration des données : scripts/migrate_state.py.

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from server.common.paths import NERON_DATA_DIR

DEFAULT_DATABASE_PATH = Path(
    os.getenv("NERON_GOAL_STATE_DB", str(NERON_DATA_DIR / "goal" / "goal_state.sqlite3"))
)

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def get_path_lock(path: Path | str) -> threading.RLock:
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class SQLiteStore:
    def __init__(self, path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = get_path_lock(self.path)
        self.migrate()

    def migrate(self) -> None:
        statements = (
            """
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    progress REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_projects_goal_id
                    ON projects(goal_id)
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_projects_plan_id
                    ON projects(plan_id)
            """,
            """
                CREATE TABLE IF NOT EXISTS workflows (
                    plan_id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_workflows_goal_id
                    ON workflows(goal_id)
            """,
            """
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT,
                    status TEXT,
                    occurred_at TEXT,
                    error TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (owner_type, owner_id, position)
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS test_results (
                    project_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    name TEXT,
                    returncode INTEGER,
                    ran_at TEXT,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (project_id, position)
                )
            """,
            """
                CREATE TABLE IF NOT EXISTS goal_runs (
                    goal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    progress INTEGER NOT NULL DEFAULT 0,
                    plan_id TEXT,
                    project_id TEXT,
                    agent_slug TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    error TEXT,
                    metadata_json TEXT NOT NULL
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_goal_runs_status
                    ON goal_runs(status)
            """,
            """
                CREATE TABLE IF NOT EXISTS goal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_goal_events_goal_id
                    ON goal_events(goal_id, id)
            """,
            """
                CREATE TABLE IF NOT EXISTS scheduler_tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_status_priority
                    ON scheduler_tasks(status, priority, created_at)
            """,
            """
                CREATE TABLE IF NOT EXISTS workflow_tasks (
                    task_id TEXT PRIMARY KEY,
                    goal_id TEXT,
                    plan_id TEXT,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_workflow_tasks_plan_id
                    ON workflow_tasks(plan_id)
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_workflow_tasks_goal_id
                    ON workflow_tasks(goal_id)
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_workflow_tasks_status
                    ON workflow_tasks(status)
            """,
            """
                CREATE TABLE IF NOT EXISTS agent_runtime_executions (
                    execution_id TEXT PRIMARY KEY,
                    agent_slug TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_ms REAL,
                    result TEXT,
                    error TEXT
                )
            """,
            """
                CREATE INDEX IF NOT EXISTS idx_agent_runtime_executions_started
                    ON agent_runtime_executions(started_at DESC)
            """,
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)

    def upsert_goal(self, goal: dict[str, Any]) -> None:
        goal_id = str(goal["id"])
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO goals (
                    goal_id, status, current_step, progress,
                    created_at, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    status = excluded.status,
                    current_step = excluded.current_step,
                    progress = excluded.progress,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    goal_id,
                    str(goal.get("status") or "pending"),
                    str(goal.get("current_step") or goal.get("status") or "pending"),
                    float(goal.get("progress") or 0),
                    float(goal.get("created_at") or 0),
                    float(goal.get("updated_at") or goal.get("created_at") or 0),
                    self._dump(goal),
                ),
            )

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        return self._fetch_payload(
            "SELECT payload FROM goals WHERE goal_id = ?",
            (goal_id,),
        )

    def list_goals(self) -> list[dict[str, Any]]:
        return self._fetch_payloads(
            "SELECT payload FROM goals ORDER BY created_at ASC"
        )

    def upsert_project(self, project: dict[str, Any]) -> None:
        project_id = str(project["project_id"])
        metadata = project.get("metadata") or {}
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, goal_id, plan_id, status, current_step,
                    progress, created_at, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    goal_id = excluded.goal_id,
                    plan_id = excluded.plan_id,
                    status = excluded.status,
                    current_step = excluded.current_step,
                    progress = excluded.progress,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    project_id,
                    self._optional_text(metadata.get("goal_id")),
                    self._optional_text(metadata.get("plan_id")),
                    str(project.get("status") or "pending"),
                    self._optional_text(project.get("current_step")),
                    int(project.get("progress") or 0),
                    float(project.get("created_at") or 0),
                    float(project.get("updated_at") or project.get("created_at") or 0),
                    self._dump(project),
                ),
            )
            self._replace_steps(
                connection,
                owner_type="project",
                owner_id=project_id,
                steps=list(project.get("steps") or []),
            )
            connection.execute(
                "DELETE FROM test_results WHERE project_id = ?",
                (project_id,),
            )
            for position, result in enumerate(project.get("test_results") or []):
                connection.execute(
                    """
                    INSERT INTO test_results (
                        project_id, position, name, returncode, ran_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        position,
                        self._optional_text(result.get("name")),
                        result.get("returncode"),
                        self._optional_text(result.get("ran_at")),
                        self._dump(result),
                    ),
                )

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self._fetch_payload(
            "SELECT payload FROM projects WHERE project_id = ?",
            (project_id,),
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return self._fetch_payloads(
            "SELECT payload FROM projects ORDER BY created_at ASC"
        )

    def find_project_by_tracking(
        self,
        *,
        goal_id: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, Any] | None:
        if goal_id:
            result = self._fetch_payload(
                """
                SELECT payload FROM projects
                WHERE goal_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (goal_id,),
            )
            if result is not None:
                return result
        if plan_id:
            return self._fetch_payload(
                """
                SELECT payload FROM projects
                WHERE plan_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (plan_id,),
            )
        return None

    def upsert_workflow(self, workflow: dict[str, Any]) -> None:
        plan_id = str(workflow["id"])
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflows (
                    plan_id, goal_id, status, current_step, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    goal_id = excluded.goal_id,
                    status = excluded.status,
                    current_step = excluded.current_step,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    plan_id,
                    self._optional_text(workflow.get("goal_id")),
                    str(workflow.get("status") or "pending"),
                    self._optional_text(workflow.get("current_step")),
                    self._workflow_timestamp(workflow),
                    self._dump(workflow),
                ),
            )
            self._replace_steps(
                connection,
                owner_type="workflow",
                owner_id=plan_id,
                steps=list(workflow.get("steps") or []),
            )

    def get_workflow(self, plan_id: str) -> dict[str, Any] | None:
        return self._fetch_payload(
            "SELECT payload FROM workflows WHERE plan_id = ?",
            (plan_id,),
        )

    def find_workflow_by_goal(self, goal_id: str) -> dict[str, Any] | None:
        return self._fetch_payload(
            """
            SELECT payload FROM workflows
            WHERE goal_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (goal_id,),
        )

    def list_workflows(self) -> list[dict[str, Any]]:
        return self._fetch_payloads(
            "SELECT payload FROM workflows ORDER BY updated_at ASC"
        )

    def upsert_workflow_task(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        metadata = task.get("metadata") or {}
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_tasks (
                    task_id, goal_id, plan_id, status, priority,
                    created_at, updated_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    goal_id = excluded.goal_id,
                    plan_id = excluded.plan_id,
                    status = excluded.status,
                    priority = excluded.priority,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (
                    task_id,
                    self._optional_text(task.get("goal_id") or metadata.get("goal_id")),
                    self._optional_text(task.get("plan_id") or metadata.get("plan_id")),
                    str(task.get("status") or "pending"),
                    str(task.get("priority") or "medium"),
                    str(task.get("created_at") or ""),
                    str(task.get("updated_at") or task.get("created_at") or ""),
                    self._dump(task),
                ),
            )

    def get_workflow_task(self, task_id: str) -> dict[str, Any] | None:
        return self._fetch_payload(
            "SELECT payload FROM workflow_tasks WHERE task_id = ?",
            (task_id,),
        )

    def list_workflow_tasks(self) -> list[dict[str, Any]]:
        return self._fetch_payloads(
            "SELECT payload FROM workflow_tasks ORDER BY created_at ASC"
        )

    def delete_workflow_task(self, task_id: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM workflow_tasks WHERE task_id = ?",
                (task_id,),
            )
            return cursor.rowcount > 0

    def replace_workflow_tasks(self, tasks: list[dict[str, Any]]) -> None:
        task_ids = [str(task["id"]) for task in tasks if task.get("id")]
        with self._transaction() as connection:
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                connection.execute(
                    f"DELETE FROM workflow_tasks WHERE task_id NOT IN ({placeholders})",
                    task_ids,
                )
            else:
                connection.execute("DELETE FROM workflow_tasks")
        for task in tasks:
            self.upsert_workflow_task(task)

    def workflow_steps(
        self,
        *,
        owner_type: str,
        owner_id: str,
    ) -> list[dict[str, Any]]:
        return self._fetch_payloads(
            """
            SELECT payload FROM workflow_steps
            WHERE owner_type = ? AND owner_id = ?
            ORDER BY position ASC
            """,
            (owner_type, owner_id),
        )

    def create_goal_run(
        self,
        run: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO goal_runs (
                    goal_id, status, current_step, progress, plan_id,
                    project_id, agent_slug, created_at, updated_at,
                    started_at, finished_at, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO NOTHING
                """,
                self._goal_run_values(run),
            )
            if connection.execute(
                "SELECT changes()"
            ).fetchone()[0]:
                self._insert_goal_event(connection, event)

    def update_goal_run(
        self,
        goal_id: str,
        updates: dict[str, Any],
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        allowed = {
            "status",
            "current_step",
            "progress",
            "plan_id",
            "project_id",
            "agent_slug",
            "updated_at",
            "started_at",
            "finished_at",
            "error",
            "metadata_json",
        }
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            values.append(
                self._dump(value)
                if key == "metadata_json" and isinstance(value, dict)
                else value
            )
        if not assignments:
            return self.get_goal_run(goal_id)

        with self._transaction() as connection:
            cursor = connection.execute(
                f"UPDATE goal_runs SET {', '.join(assignments)} WHERE goal_id = ?",
                (*values, goal_id),
            )
            if cursor.rowcount == 0:
                return None
            if event is not None:
                self._insert_goal_event(connection, event)
            row = connection.execute(
                "SELECT * FROM goal_runs WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        return self._goal_run_from_row(row) if row else None

    def get_goal_run(self, goal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM goal_runs WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        return self._goal_run_from_row(row) if row else None

    def list_goal_runs(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM goal_runs ORDER BY created_at DESC"
            ).fetchall()
        return [self._goal_run_from_row(row) for row in rows]

    def list_goal_events(self, goal_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, goal_id, step, status, message, payload_json, created_at
                FROM goal_events
                WHERE goal_id = ?
                ORDER BY id ASC
                """,
                (goal_id,),
            ).fetchall()
        return [self._goal_event_from_row(row) for row in rows]

    def interrupt_running_goal_runs(self, now: float) -> list[str]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT goal_id FROM goal_runs WHERE status = 'running'"
            ).fetchall()
            goal_ids = [str(row["goal_id"]) for row in rows]
            for goal_id in goal_ids:
                connection.execute(
                    """
                    UPDATE goal_runs
                    SET status = 'interrupted',
                        current_step = 'interrupted',
                        updated_at = ?,
                        finished_at = ?,
                        error = COALESCE(error, 'Execution interrupted by process restart')
                    WHERE goal_id = ?
                    """,
                    (now, now, goal_id),
                )
                self._insert_goal_event(
                    connection,
                    {
                        "goal_id": goal_id,
                        "step": "interrupted",
                        "status": "interrupted",
                        "message": "Execution interrupted by process restart",
                        "payload": {},
                        "created_at": now,
                    },
                )
        return goal_ids

    def journal_mode(self) -> str:
        with self._connect() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""

    def busy_timeout(self) -> int:
        with self._connect() as connection:
            row = connection.execute("PRAGMA busy_timeout").fetchone()
        return int(row[0]) if row else 0

    def _replace_steps(
        self,
        connection: sqlite3.Connection,
        *,
        owner_type: str,
        owner_id: str,
        steps: list[dict[str, Any]],
    ) -> None:
        connection.execute(
            "DELETE FROM workflow_steps WHERE owner_type = ? AND owner_id = ?",
            (owner_type, owner_id),
        )
        for position, step in enumerate(steps):
            connection.execute(
                """
                INSERT INTO workflow_steps (
                    owner_type, owner_id, position, name, status,
                    occurred_at, error, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner_type,
                    owner_id,
                    position,
                    self._optional_text(step.get("name") or step.get("title")),
                    self._optional_text(step.get("status")),
                    self._optional_text(step.get("at") or step.get("completed_at")),
                    self._optional_text(step.get("error")),
                    self._dump(step),
                ),
            )

    def _goal_run_values(self, run: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(run["goal_id"]),
            str(run.get("status") or "queued"),
            self._optional_text(run.get("current_step")),
            int(run.get("progress") or 0),
            self._optional_text(run.get("plan_id")),
            self._optional_text(run.get("project_id")),
            self._optional_text(run.get("agent_slug")),
            float(run.get("created_at") or time.time()),
            float(run.get("updated_at") or time.time()),
            run.get("started_at"),
            run.get("finished_at"),
            self._optional_text(run.get("error")),
            self._dump(run.get("metadata") or {}),
        )

    def _insert_goal_event(
        self,
        connection: sqlite3.Connection,
        event: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO goal_events (
                goal_id, step, status, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(event["goal_id"]),
                str(event.get("step") or "unknown"),
                str(event.get("status") or "unknown"),
                self._optional_text(event.get("message")),
                self._dump(event.get("payload") or {}),
                float(event.get("created_at") or time.time()),
            ),
        )

    def _goal_run_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "goal_id": str(row["goal_id"]),
            "status": str(row["status"]),
            "current_step": row["current_step"],
            "progress": int(row["progress"]),
            "plan_id": row["plan_id"],
            "project_id": row["project_id"],
            "agent_slug": row["agent_slug"],
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "metadata": self._load_payload(row["metadata_json"]),
        }

    def _goal_event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "goal_id": str(row["goal_id"]),
            "step": str(row["step"]),
            "status": str(row["status"]),
            "message": row["message"],
            "payload": self._load_payload(row["payload_json"]),
            "created_at": float(row["created_at"]),
        }

    def _fetch_payload(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return self._load_payload(row["payload"]) if row else None

    def _fetch_payloads(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._load_payload(row["payload"]) for row in rows]

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def _workflow_timestamp(self, workflow: dict[str, Any]) -> float:
        for key in ("updated_at", "finished_at", "execution_started_at", "created_at"):
            value = workflow.get(key)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
        return time.time()

    def _dump(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _load_payload(self, payload: str) -> dict[str, Any]:
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}

    def _optional_text(self, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return str(value)
