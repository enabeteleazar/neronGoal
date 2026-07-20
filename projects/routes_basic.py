from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from goal.infra.security import require_api_key
from goal.projects.manager import get_project_manager

router = APIRouter(tags=["projects"], dependencies=[Depends(require_api_key)])


@router.get("/projects")
async def list_projects(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    projects = get_project_manager().list_projects(status=status, limit=limit)
    return {"count": len(projects), "projects": projects}


@router.get("/projects/search")
async def search_projects(query: str, limit: int = 10) -> dict[str, Any]:
    projects = get_project_manager().find_project_by_query(query, limit=limit)
    return {"count": len(projects), "projects": projects}
