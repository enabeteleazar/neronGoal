from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.storage.sqlite_store import get_path_lock


GOALS_PATH = Path("/etc/neron/data/goals_state.json")


def load_goals_state() -> dict[str, Any]:
    with get_path_lock(GOALS_PATH):
        if GOALS_PATH.exists():
            try:
                return json.loads(GOALS_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass

    return {
        "active_goal_id": None,
        "goals": [],
        "last_update": None,
    }


def save_goals_state(data: dict[str, Any]) -> None:
    with get_path_lock(GOALS_PATH):
        GOALS_PATH.parent.mkdir(parents=True, exist_ok=True)

        tmp = GOALS_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(GOALS_PATH)


def load_legacy_goal_system_state() -> dict[str, Any]:
    legacy_path = GOALS_PATH.with_name("goals.json")
    with get_path_lock(legacy_path):
        if legacy_path.exists():
            try:
                data = json.loads(legacy_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                pass
    return {}
