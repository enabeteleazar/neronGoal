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
from server.common.config import env_int
from server.common.paths import service_version
from server.common.registry.client import RegistryClient

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

def _topology_path() -> Path:
    root = Path(os.getenv("NERON_ROOT", "/etc/neronOS"))
    return Path(os.getenv("NERON_SERVER_CONFIG", str(root / "neron.server.yaml")))


def _load_topology_nodes() -> dict[str, Any]:
    path = _topology_path()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        nodes = data.get("nodes") or {}
        if not isinstance(nodes, dict):
            raise ValueError("'nodes' n'est pas un mapping")
        return nodes
    except FileNotFoundError:
        logger.warning(
            "Topologie introuvable (%s) — repli sur variables d'env/défauts", path
        )
    except Exception as exc:  # pyyaml manquant, fichier invalide, etc.
        logger.warning("Topologie illisible (%s) : %s", path, exc)
    return {}


def _service_binding() -> tuple[str, int, str]:
    """Renvoie (host, port, core_url) pour l'enregistrement au registre."""
    nodes = _load_topology_nodes()
    goal_node = nodes.get("goal") or {}
    core_node = nodes.get("core") or {}

    host = os.getenv("NERON_SERVICE_HOST") or str(goal_node.get("host") or "127.0.1.3")
    try:
        default_port = int(goal_node.get("port") or 8030)
    except (TypeError, ValueError):
        default_port = 8030
    port = env_int("NERON_SERVICE_PORT", default_port)

    core_url = os.getenv("NERON_CORE_URL", "").strip()
    if not core_url and core_node.get("host"):
        core_url = f"http://{core_node['host']}:{core_node.get('port', 8010)}"
    if not core_url:
        core_url = "http://127.0.1.1:8010"

    return host, port, core_url


# ═══════════════════════════════════════════════════════════════════
# Authentification — même convention que le reste du cluster :
# Authorization: Bearer $NERON_API_KEY. /health et /status restent
# ouverts (sondes watchdog) ; tout le reste est protégé.
# ═══════════════════════════════════════════════════════════════════

def create_registry_client(host: str, port: int, core_url: str) -> RegistryClient:
    # Phase 3 : ces capabilities sont désormais honnêtes — goals/planning/
    # projects/tasks sont réellement montés et exécutés par ce service.
    # "agent_creation" reste vraie en mode dégradé (agents.factory.
    # build_orchestrator indisponible hors machine avec le monolithe).
    return RegistryClient(
        service_name=SERVICE_NAME,
        version=VERSION,
        host=host,
        port=port,
        core_url=core_url,
        capabilities=["goal_execution", "planning", "agent_creation", "task_loop"],
        metadata={},
    )


@asynccontextmanager
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


app = FastAPI(
    title="NéronOS Goal",
    version=VERSION,
    lifespan=lifespan,
)

# Phase 3 : l'usine est branchée sur la vitrine. Chaque router porte déjà
# sa propre dépendance require_api_key (voir goal.infra.security).
from goal.goals.routes import router as goals_router
from goal.projects.routes import router as projects_router
from goal.system.routes import router as tasks_router

app.include_router(goals_router)
app.include_router(projects_router)
app.include_router(tasks_router)



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
        "goal_count": len(get_goal_manager().list_goals()),
    }
