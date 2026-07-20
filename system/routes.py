"""Routes HTTP pour le module system (tâches).

Ajoutées en phase 3 : avant, get_task_manager() n'était appelable que par
import direct depuis le process du core. Ces trois routes remplacent ces
imports par de vrais appels HTTP inter-services.

NOTE (bug corrigé au passage) : TaskManager.get_status_summary() renvoie
{total, active, running, by_status{...}, by_priority{...}} — mais le code
appelant côté core lisait summary['pending']/['done']/['failed']/['cancelled']
à plat, des clés qui n'ont jamais existé dans ce dict (KeyError latent).
Le mapping ci-dessous expose la forme à plat que l'appelant attend
réellement, sans changer la représentation interne du TaskManager.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from goal.infra.security import require_api_key
from goal.system.task_manager import get_task_manager

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_api_key)])


@router.get("/summary")
async def task_summary() -> dict[str, Any]:
    raw = get_task_manager().get_status_summary()
    by_status = raw.get("by_status", {})
    return {
        "total": raw.get("total", 0),
        "active": raw.get("active", 0),
        "running": raw.get("running", 0),
        "pending": by_status.get("pending", 0),
        "done": by_status.get("done", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
        "by_status": by_status,
        "by_priority": raw.get("by_priority", {}),
    }


@router.get("/next")
async def next_task() -> dict[str, Any]:
    task = get_task_manager().get_next_task()
    if task is None:
        raise HTTPException(status_code=404, detail="No pending task")
    return {"task": task}


@router.post("/next/start")
async def start_next_task() -> dict[str, Any]:
    task = get_task_manager().start_next_task()
    if task is None:
        raise HTTPException(status_code=404, detail="No pending task to start")
    return {"task": task}
