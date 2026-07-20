"""Migration one-shot : copie les tables du domaine goal depuis la base
partagée du core (neron_state.sqlite3) vers la base propre au goal
(goal_state.sqlite3).

À lancer SERVICE ARRÊTÉ :
    sudo systemctl stop neron-goal
    /etc/neronOS/venv/bin/python -m goal.scripts.migrate_state
    sudo systemctl start neron-goal

Idempotent : INSERT OR IGNORE, relançable sans risque.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from server.common.paths import NERON_DATA_DIR
from goal.infra.sqlite_store import DEFAULT_DATABASE_PATH, SQLiteStore

GOAL_TABLES = [
    "goals",
    "projects",
    "workflows",
    "workflow_steps",
    "workflow_tasks",
    "goal_runs",
    "goal_events",
    "test_results",
    "scheduler_tasks",
    "agent_runtime_executions",
]


def migrate(source: Path | None = None, target: Path | None = None) -> dict[str, int]:
    source = source or Path(
        os.getenv("NERON_STATE_DB", str(NERON_DATA_DIR / "neron_state.sqlite3"))
    )
    target = target or DEFAULT_DATABASE_PATH

    if not source.exists():
        print(f"Source absente ({source}) : rien à migrer.")
        return {}

    # Crée le schéma cible via le store lui-même (migrate() dans __init__)
    SQLiteStore(target)

    copied: dict[str, int] = {}
    conn = sqlite3.connect(target)
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'"
            )
        }
        for table in GOAL_TABLES:
            if table not in existing:
                continue
            cols = [
                row[1]
                for row in conn.execute(f"PRAGMA legacy.table_info({table})")
            ]
            target_cols = {
                row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")
            }
            common = [c for c in cols if c in target_cols]
            if not common:
                continue
            col_list = ", ".join(common)
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO main.{table} ({col_list}) "
                f"SELECT {col_list} FROM legacy.{table}"
            )
            copied[table] = cursor.rowcount
        conn.commit()
    finally:
        conn.close()

    for table, count in copied.items():
        print(f"  {table:<28} {count:>6} ligne(s) copiée(s)")
    print(f"Migration terminée : {source} -> {target}")
    return copied


if __name__ == "__main__":
    sys.exit(0 if migrate() is not None else 1)
