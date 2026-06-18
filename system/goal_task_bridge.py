from __future__ import annotations

from typing import Any

from goal.system.task_manager import get_task_manager


ACTIVE_TASK_STATUSES = {
    "pending",
    "running",
}


def _find_existing_goal_task(goal_id: str) -> dict[str, Any] | None:
    manager = get_task_manager()

    for task in manager.list_tasks():
        metadata = task.get("metadata", {})

        if metadata.get("goal_id") != goal_id:
            continue

        if task.get("status") not in ACTIVE_TASK_STATUSES:
            continue

        return task

    return None


def create_task_from_goal(goal: Any) -> dict[str, Any]:
    """
    Convertit un objectif canonique en tâche persistante.
    Empêche les doublons actifs pour un même goal_id.
    """

    manager = get_task_manager()

    if isinstance(goal, dict):
        goal_id = goal.get("id")
        title = goal.get("title") or goal.get("name") or "Objectif sans titre"
        description = goal.get("description", "")
        priority = goal.get("priority", "high")
    else:
        goal_id = getattr(goal, "id", None)
        title = getattr(goal, "title", None) or getattr(goal, "name", None) or "Objectif sans titre"
        description = getattr(goal, "description", "")
        priority = getattr(goal, "priority", "high")

    if goal_id:
        existing = _find_existing_goal_task(goal_id)

        if existing:
            return {
                **existing,
                "_existing": True,
            }

    return manager.create_task(
        title=f"Objectif: {title}",
        description=description,
        priority=priority if priority in {"low", "normal", "high", "critical"} else "high",
        source="goal_system",
        goal_id=str(goal_id) if goal_id else None,
        metadata={
            "source": "goal_system",
            "goal_id": goal_id,
            "goal_title": title,
        },
    )
