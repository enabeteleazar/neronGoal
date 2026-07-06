from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import logging
from threading import RLock
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.common.registry.client import RegistryClient


logger = logging.getLogger("goal.app")
VERSION = "0.1.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1)
    description: str = ""
    priority: str = "medium"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value


class GoalStore:
    """Small process-local catalogue for the autonomous Goal MVP."""

    def __init__(self) -> None:
        self._goals: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def create(self, request: GoalCreateRequest) -> dict[str, Any]:
        now = _utc_now()
        goal = {
            "id": f"goal_{uuid4().hex[:12]}",
            "title": request.title,
            "description": request.description,
            "priority": request.priority,
            "status": "queued",
            "metadata": deepcopy(request.metadata),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._goals[goal["id"]] = goal
        return deepcopy(goal)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(list(self._goals.values()))

    def get(self, goal_id: str) -> dict[str, Any] | None:
        with self._lock:
            goal = self._goals.get(goal_id)
            return deepcopy(goal) if goal is not None else None

    def cancel(self, goal_id: str) -> dict[str, Any] | None:
        with self._lock:
            goal = self._goals.get(goal_id)
            if goal is None:
                return None
            if goal["status"] not in {"completed", "failed", "cancelled"}:
                goal["status"] = "cancelled"
                goal["updated_at"] = _utc_now()
            return deepcopy(goal)

    def clear(self) -> None:
        with self._lock:
            self._goals.clear()


goal_store = GoalStore()


def create_registry_client() -> RegistryClient:
    return RegistryClient(
        service_name="goal",
        version=VERSION,
        host="localhost",
        port=8030,
        capabilities=["goal_execution", "planning", "agent_creation", "task_loop"],
        metadata={},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.started_at = time.monotonic()
    registry_client = create_registry_client()
    app.state.registry_client = registry_client
    await registry_client.start()
    logger.info("Goal daemon started on port 8030")
    try:
        yield
    finally:
        await registry_client.stop()
        logger.info("Goal daemon stopped")


app = FastAPI(
    title="NéronOS Goal",
    version=VERSION,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": "goal", "status": "healthy"}


@app.get("/status")
async def service_status() -> dict[str, str | float | int]:
    started_at = getattr(app.state, "started_at", time.monotonic())
    return {
        "service": "goal",
        "status": "running",
        "uptime": round(max(0.0, time.monotonic() - started_at), 3),
        "goal_count": len(goal_store.list()),
    }


@app.get("/goals")
async def list_goals() -> dict[str, Any]:
    goals = goal_store.list()
    return {"count": len(goals), "goals": goals}


@app.post("/goals", status_code=status.HTTP_202_ACCEPTED)
async def create_goal(request: GoalCreateRequest) -> dict[str, Any]:
    return {"goal": goal_store.create(request)}


@app.get("/goals/{goal_id}")
async def get_goal(goal_id: str) -> dict[str, Any]:
    goal = goal_store.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}


@app.post("/goals/{goal_id}/cancel")
async def cancel_goal(goal_id: str) -> dict[str, Any]:
    goal = goal_store.cancel(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}
