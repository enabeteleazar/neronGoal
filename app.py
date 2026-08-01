from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status

from goal.infra.security import expected_api_key as _expected_api_key
from goal.infra.security import require_api_key
from server.common.paths import service_version
from server.common.service import create_service_app

from goal.goals.goal_manager import get_goal_manager


logger = logging.getLogger("goal.app")
VERSION = service_version(__file__)
SERVICE_NAME = "goal"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════
# Topologie — env d'abord, puis neron.server.yaml, puis défauts sûrs.
# Plus aucun "localhost" codé en dur.
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# Authentification — même convention que le reste du cluster :
# Authorization: Bearer $NERON_API_KEY. /health et /status restent
# ouverts (sondes watchdog) ; tout le reste est protégé.
# ═══════════════════════════════════════════════════════════════════

async def lifespan(app: FastAPI):
    app.state.started_at = time.monotonic()
    app.state.registry_client = None

    if not _expected_api_key():
        logger.warning(
            "NERON_API_KEY absente : les endpoints /goals ne sont PAS protégés"
        )

    host, port, core_url = _service_binding()
    try:
        registry_client = create_registry_client(host, port, core_url)
        await registry_client.start()
        app.state.registry_client = registry_client
        logger.info(
            "Goal daemon démarré (annonce %s:%s, core=%s)", host, port, core_url
        )
    except Exception:
        # Le registre ne doit jamais empêcher l'API de servir.
        logger.exception(
            "Échec d'initialisation du client registre — "
            "l'API goal reste disponible sans enregistrement"
        )

    try:
        yield
    finally:
        if app.state.registry_client is not None:
            await app.state.registry_client.stop()
        logger.info("Goal daemon stopped")


@asynccontextmanager
async def _setup(app: FastAPI):
    if not _expected_api_key():
        logger.warning(
            "NERON_API_KEY absente : les endpoints /goals ne sont PAS protégés"
        )
    yield


app = create_service_app(
    name="goal",
    title="NéronOS Goal",
    version=VERSION,
    capabilities=["goal_execution", "planning", "agent_creation", "task_loop"],
    setup=_setup,
)

# Phase 3 : l'usine est branchée sur la vitrine. Chaque router porte déjà
# sa propre dépendance require_api_key (voir goal.infra.security).
from goal.goals.routes import router as goals_router
from goal.projects.routes import router as projects_router
from goal.system.routes import router as tasks_router

app.include_router(goals_router)
app.include_router(projects_router)
app.include_router(tasks_router)



@app.get("/status")
async def service_status() -> dict[str, str | float | int]:
    started_at = getattr(app.state, "started_at", time.monotonic())
    return {
        "service": "goal",
        "status": "running",
        "uptime": round(max(0.0, time.monotonic() - started_at), 3),
        "goal_count": len(get_goal_manager().list_goals()),
    }
