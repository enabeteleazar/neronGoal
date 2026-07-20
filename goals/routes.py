from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from goal.infra.security import require_api_key
from goal.goals.background_runner import get_goal_background_runner
from goal.goals.execution_engine import get_goal_execution_engine
from goal.goals.goal_manager import get_goal_manager
from goal.goals.goal_orchestrator import get_goal_orchestrator


# Phase 2 : plus AUCUN import de core.pipeline. Le CoreOrchestrator ne faisait
# qu'appeler les composants goal (queue -> enqueue -> submit) : on les appelle
# directement, la frontière de service est rétablie.

router = APIRouter(tags=["goals"], dependencies=[Depends(require_api_key)])


class GoalCreateRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    source: str = "api"
    metadata: dict[str, Any] = {}


class GoalProgressRequest(BaseModel):
    progress: float


class GoalRunRequest(BaseModel):
    objective: str
    source: str = "api"


class GoalAliasRequest(BaseModel):
    goal: str | None = None
    objective: str | None = None
    source: str = "api"


@router.post("/goals/run")
async def run_goal(payload: GoalRunRequest) -> dict[str, Any]:
    """Exécution synchrone d'un objectif (bloque jusqu'au verdict)."""
    objective = payload.objective.strip()
    if not objective:
        raise HTTPException(status_code=422, detail="objective is required")
    return await get_goal_orchestrator().run_goal(objective, source=payload.source)


@router.post("/goal", status_code=status.HTTP_202_ACCEPTED)
async def run_goal_alias(payload: GoalAliasRequest) -> dict[str, Any]:
    """File d'attente asynchrone : accepte, enregistre, exécute en tâche de fond."""
    objective = (payload.goal or payload.objective or "").strip()
    if not objective:
        raise HTTPException(status_code=422, detail="goal or objective is required")

    goal = get_goal_orchestrator().queue_goal(objective, source=payload.source)
    goal_id = str(goal["id"])

    get_goal_execution_engine().enqueue_goal(
        goal_id,
        objective,
        payload.source,
        dict(goal.get("metadata") or {}),
    )
    get_goal_background_runner().submit(
        goal_id=goal_id,
        objective=objective,
        source=payload.source,
    )
    return {
        "accepted": True,
        "goal_id": goal_id,
        "status": "queued",
        "status_url": f"/goal/{goal_id}/status",
    }


@router.get("/goals")
async def list_goals() -> dict[str, Any]:
    engine_goals = get_goal_execution_engine().list_goals()
    legacy_goals = get_goal_manager().list_goals()
    run_ids = {goal["goal_id"] for goal in engine_goals}
    goals = engine_goals + [
        goal for goal in legacy_goals if str(goal.get("id") or "") not in run_ids
    ]
    return {"count": len(goals), "goals": goals}


@router.get("/goals/active")
async def active_goal() -> dict[str, Any]:
    return {"active_goal": get_goal_manager().get_active_goal()}


@router.get("/goal/{goal_id}/status")
async def goal_status(goal_id: str) -> dict[str, Any]:
    result = get_goal_orchestrator().get_goal_status(goal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return result


@router.get("/goal/{goal_id}/events")
async def goal_events(goal_id: str) -> dict[str, Any]:
    engine = get_goal_execution_engine()
    if engine.get_goal_status(goal_id) is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal_id": goal_id, "events": engine.get_goal_events(goal_id)}


@router.post("/goals")
async def create_goal(payload: GoalCreateRequest) -> dict[str, Any]:
    goal = get_goal_manager().create_goal(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        source=payload.source,
        metadata=payload.metadata,
    )
    return {"goal": goal}


@router.post("/goals/{goal_id}/complete")
async def complete_goal(goal_id: str) -> dict[str, Any]:
    goal = get_goal_manager().update_status(goal_id, "completed")
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}


@router.post("/goals/{goal_id}/fail")
async def fail_goal(goal_id: str) -> dict[str, Any]:
    goal = get_goal_manager().update_status(goal_id, "failed")
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}


@router.post("/goals/{goal_id}/progress")
async def update_progress(goal_id: str, payload: GoalProgressRequest) -> dict[str, Any]:
    goal = get_goal_manager().update_progress(goal_id, payload.progress)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}
