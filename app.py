from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.common.config import env_int
from server.common.registry.client import RegistryClient


logger = logging.getLogger("goal.app")
VERSION = "0.1.0"
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

def _expected_api_key() -> str:
    return os.getenv("NERON_API_KEY", "").strip()


async def require_api_key(request: Request) -> None:
    expected = _expected_api_key()
    if not expected:
        # Clé absente : comportement historique conservé (ouvert), mais
        # signalé au démarrage dans lifespan() plutôt qu'à chaque requête.
        return
    header = request.headers.get("Authorization", "")
    provided = header.removeprefix("Bearer ").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


# ═══════════════════════════════════════════════════════════════════
# Catalogue MVP (inchangé — remplacé en phase 3)
# ═══════════════════════════════════════════════════════════════════

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


def create_registry_client(host: str, port: int, core_url: str) -> RegistryClient:
    # NOTE : les capabilities restent celles annoncées historiquement pour ne
    # pas casser une éventuelle découverte côté core ; elles deviendront
    # honnêtes en phase 3, quand l'exécution réelle sera montée ici.
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


@app.get("/goals", dependencies=[Depends(require_api_key)])
async def list_goals() -> dict[str, Any]:
    goals = goal_store.list()
    return {"count": len(goals), "goals": goals}


@app.post(
    "/goals",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_goal(request: GoalCreateRequest) -> dict[str, Any]:
    return {"goal": goal_store.create(request)}


@app.get("/goals/{goal_id}", dependencies=[Depends(require_api_key)])
async def get_goal(goal_id: str) -> dict[str, Any]:
    goal = goal_store.get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}


@app.post("/goals/{goal_id}/cancel", dependencies=[Depends(require_api_key)])
async def cancel_goal(goal_id: str) -> dict[str, Any]:
    goal = goal_store.cancel(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {"goal": goal}
