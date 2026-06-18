from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from goal.goals.background_runner import get_goal_background_runner
from goal.goals.execution_engine import get_goal_execution_engine
from goal.goals.goal_manager import get_goal_manager
from goal.goals.goal_orchestrator import get_goal_orchestrator
from core.pipeline.orchestrator import CoreOrchestrator


router = APIRouter(tags=["goals"])


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
    return await CoreOrchestrator(
        goal_orchestrator_factory=get_goal_orchestrator,
    ).run_goal(
        payload.objective,
        source=payload.source,
    )


@router.post("/goal", status_code=status.HTTP_202_ACCEPTED)
async def run_goal_alias(payload: GoalAliasRequest) -> dict[str, Any]:
    objective = (payload.goal or payload.objective or "").strip()
    if not objective:
        raise HTTPException(status_code=422, detail="goal or objective is required")

    goal = await CoreOrchestrator(
        goal_orchestrator_factory=get_goal_orchestrator,
        goal_execution_engine=get_goal_execution_engine(),
        goal_background_runner=get_goal_background_runner(),
    ).queue_goal(
        objective,
        source=payload.source,
    )
    goal_id = str(goal["id"])
    return {
        "accepted": True,
        "goal_id": goal_id,
        "status": "queued",
        "status_url": f"/goal/{goal_id}/status",
    }


@router.get("/goals")
async def list_goals() -> dict[str, Any]:
    engine_goals = get_goal_execution_engine().list_goals()
    manager = get_goal_manager()
    legacy_goals = manager.list_goals()
    run_ids = {goal["goal_id"] for goal in engine_goals}
    goals = engine_goals + [
        goal for goal in legacy_goals if str(goal.get("id") or "") not in run_ids
    ]
    return {"count": len(goals), "goals": goals}


@router.get("/goals/active")
async def active_goal() -> dict[str, Any]:
    manager = get_goal_manager()
    return {"active_goal": manager.get_active_goal()}


@router.get("/goal/{goal_id}/status")
async def goal_status(goal_id: str) -> dict[str, Any]:
    orchestrator = get_goal_orchestrator()
    status = orchestrator.get_goal_status(goal_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return status


@router.get("/goal/{goal_id}/events")
async def goal_events(goal_id: str) -> dict[str, Any]:
    engine = get_goal_execution_engine()
    if engine.get_goal_status(goal_id) is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {
        "goal_id": goal_id,
        "events": engine.get_goal_events(goal_id),
    }


@router.post("/goals")
async def create_goal(payload: GoalCreateRequest) -> dict[str, Any]:
    manager = get_goal_manager()
    goal = manager.create_goal(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        source=payload.source,
        metadata=payload.metadata,
    )
    return {"goal": goal}


@router.post("/goals/{goal_id}/complete")
async def complete_goal(goal_id: str) -> dict[str, Any]:
    manager = get_goal_manager()
    goal = manager.update_status(goal_id, "completed")

    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    return {"goal": goal}


@router.post("/goals/{goal_id}/fail")
async def fail_goal(goal_id: str) -> dict[str, Any]:
    manager = get_goal_manager()
    goal = manager.update_status(goal_id, "failed")

    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    return {"goal": goal}


@router.post("/goals/{goal_id}/progress")
async def update_progress(goal_id: str, payload: GoalProgressRequest) -> dict[str, Any]:
    manager = get_goal_manager()
    goal = manager.update_progress(goal_id, payload.progress)

    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")

    return {"goal": goal}
