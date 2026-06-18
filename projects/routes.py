from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.api.auth import verify_api_key
from agents.factory.agent_creator import AgentCreator
from agents.factory.build_orchestrator import AgentBuildOrchestrator
from agents.factory.agent_manager import AgentManager
from agents.factory.promotion import AgentPromotionService
from agents.factory.validator import validate_agent
from agents.runtime.runtime import get_agent_runtime
from modules.evolution.codex_runner import CodexRunner
from goal.projects.manager import get_project_manager


router = APIRouter(tags=["projects"], dependencies=[Depends(verify_api_key)])


class AgentBuildRequest(BaseModel):
    query: str
    requested_by: str = "api"
    source_channel: str = "api"
    build_mode: Literal["deterministic", "codex", "hybrid"] = "deterministic"


class AgentProposalApprovalRequest(BaseModel):
    mode: Literal["deterministic", "codex"] = "deterministic"


class AgentUpdateRequest(BaseModel):
    request: str = ""


class AgentRenameRequest(BaseModel):
    new_name: str


class AgentRollbackRequest(BaseModel):
    backup_path: str | None = None


@router.get("/projects")
async def list_projects(status: str | None = None, limit: int = 100) -> dict:
    manager = get_project_manager()
    projects = manager.list_projects(status=status, limit=limit)
    return {"count": len(projects), "projects": projects}


@router.get("/projects/search")
async def search_projects(q: str = Query(..., min_length=1), limit: int = 10) -> dict:
    manager = get_project_manager()
    projects = manager.find_project_by_query(q, limit=limit)
    return {"count": len(projects), "projects": projects}


@router.get("/projects/diagnostics/failures")
async def diagnose_project_failures(limit: int = 10) -> dict:
    manager = get_project_manager()
    return manager.diagnose_recent_failures(limit=limit)


@router.get("/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    manager = get_project_manager()
    project = manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"project": project}


@router.post("/agents/build")
async def build_agent(payload: AgentBuildRequest) -> dict:
    orchestrator = AgentBuildOrchestrator()
    return await orchestrator.build_from_request(
        payload.query,
        requested_by=payload.requested_by,
        source_channel=payload.source_channel,
        build_mode=payload.build_mode,
    )


@router.post("/agents/proposals/{agent_request_id}/approve")
async def approve_agent_proposal(
    agent_request_id: str,
    payload: AgentProposalApprovalRequest | None = None,
) -> dict:
    mode = (payload or AgentProposalApprovalRequest()).mode
    creator = AgentCreator()
    proposal = creator.get_proposal(agent_request_id)

    if not proposal:
        raise HTTPException(status_code=404, detail="Agent proposal not found")

    if proposal.get("status") != "pending_human_validation":
        raise HTTPException(
            status_code=409,
            detail=f"Agent proposal is not pending_human_validation: {proposal.get('status')}",
        )
    if mode == "codex" and proposal.get("codex_ready") is not True:
        raise HTTPException(status_code=409, detail="Agent proposal is not codex_ready")

    approved = creator.update_proposal(
        agent_request_id,
        {
            "status": "human_approved",
            "human_validation_required": False,
            "human_approved": True,
            "code_execution_allowed": True,
        },
    )
    if not approved:
        raise HTTPException(status_code=404, detail="Agent proposal not found")

    if mode == "codex":
        return await _approve_agent_proposal_with_codex(creator, agent_request_id, approved)

    return await _approve_agent_proposal_deterministic(creator, agent_request_id, approved)


async def _approve_agent_proposal_deterministic(
    creator: AgentCreator,
    agent_request_id: str,
    approved: dict,
) -> dict:
    build_query = _build_query_from_proposal(approved)
    orchestrator = AgentBuildOrchestrator()
    build_result = await orchestrator.build_from_request(
        build_query,
        requested_by="agent_creator_approval",
        source_channel="api",
    )
    project = build_result.get("project") or {}
    created_files = list(project.get("created_files") or [])
    registered_agent = (
        project.get("registered_agent")
        or (project.get("result") or {}).get("agent")
        or (build_result.get("spec") or {}).get("name")
        or (build_result.get("agent") or {}).get("agent_name")
    )
    runtime_reload = get_agent_runtime().reload()
    errors = _build_errors(build_result)

    final_proposal = creator.update_proposal(
        agent_request_id,
        {
            "build_status": build_result.get("status"),
            "build_project_id": project.get("project_id"),
            "created_files": created_files,
            "registered_agent": registered_agent,
            "runtime_reload": runtime_reload,
            "applied_to_core": build_result.get("status") == "completed" and bool(registered_agent),
            "errors": errors,
        },
    ) or approved

    return {
        "agent_request_id": agent_request_id,
        "mode": "deterministic",
        "proposal_status": final_proposal.get("status"),
        "build_status": build_result.get("status"),
        "created_files": created_files,
        "registered_agent": registered_agent,
        "runtime_reload": runtime_reload,
        "errors": errors,
        "project": project or None,
        "build": build_result,
    }


async def _approve_agent_proposal_with_codex(
    creator: AgentCreator,
    agent_request_id: str,
    approved: dict,
) -> dict:
    prompt = _codex_prompt_from_proposal(approved)
    runner = CodexRunner()
    codex_result = await runner.run_codex(prompt, f"agent_creator_{agent_request_id}")
    codex_payload = codex_result.to_dict()
    test_results = []
    errors: list[str] = []
    created_files: list[str] = []
    registered_agent = None
    runtime_reload = None

    if not codex_result.ok:
        errors.append(codex_result.stderr or codex_result.stdout or "codex_failed")
    else:
        test_results = [result.to_dict() for result in await runner.run_tests()]
        failed_tests = [result for result in test_results if not result.get("ok")]
        if failed_tests:
            errors.append(
                str(failed_tests[0].get("stderr") or failed_tests[0].get("stdout") or "tests_failed")
            )
        else:
            agent_file = _codex_workspace_agent_path(creator, approved)
            if not agent_file.exists():
                errors.append(f"agent_file_missing: {agent_file}")
            else:
                created_files = _existing_codex_created_files(approved, creator.project_root)
                validation = validate_agent(str(agent_file))
                if not validation.get("ok"):
                    errors.append(str(validation.get("error") or "validation_failed"))
                else:
                    runtime = get_agent_runtime()
                    promotion = AgentPromotionService(
                        generated_dir=_runtime_generated_dir(runtime),
                    ).promote(
                        agent_file,
                        requested_by="agent_creator_codex_approval",
                    )
                    if not promotion.get("ok"):
                        errors.append(str(promotion.get("error") or "promotion_failed"))
                    else:
                        destination = Path(str(promotion["destination"]))
                        created_files = _append_unique(
                            created_files,
                            _relative_to_root(destination, creator.project_root),
                        )
                        registered_agent = str(approved.get("agent_name") or agent_file.stem)
                        runtime_reload = runtime.reload()

    final_proposal = creator.update_proposal(
        agent_request_id,
        {
            "codex_mode": "manual",
            "codex_auto_run": False,
            "codex_result": codex_payload,
            "test_results": test_results,
            "build_status": "completed" if registered_agent else "failed",
            "created_files": created_files,
            "registered_agent": registered_agent,
            "runtime_reload": runtime_reload,
            "applied_to_core": bool(registered_agent),
            "errors": errors,
        },
    ) or approved

    return {
        "agent_request_id": agent_request_id,
        "mode": "codex",
        "proposal_status": final_proposal.get("status"),
        "codex_result": codex_payload,
        "test_results": test_results,
        "created_files": created_files,
        "registered_agent": registered_agent,
        "runtime_reload": runtime_reload,
        "errors": errors,
    }


@router.get("/agents")
async def list_agents() -> dict:
    runtime = get_agent_runtime()
    agents = runtime.list_agents()
    return {"count": len(agents), "agents": agents}


@router.get("/agents/registry/scan")
async def scan_agent_registry_get() -> dict:
    registry = get_agent_runtime().registry
    result = registry.scan()
    result["runtime_reload"] = get_agent_runtime().reload()
    result["runtime_reloaded"] = True
    return result


@router.post("/agents/registry/scan")
async def scan_agent_registry_post() -> dict:
    return await scan_agent_registry_get()


@router.get("/agents/registry/index")
async def agent_registry_index() -> dict:
    return get_agent_runtime().registry.validation_index()

@router.get("/agents/registry/diagnostics")
async def agent_registry_diagnostics() -> dict:
    manager = get_project_manager()
    return get_agent_runtime().registry.diagnose_consistency(
        projects=manager.list_projects(limit=500),
        workspace_agents=Path("/etc/neron/workspace/agents"),
    )


@router.get("/agents/{agent_name}/status")
async def agent_status(agent_name: str) -> dict:
    return AgentManager().get_agent_status(agent_name)


@router.post("/agents/{agent_name}/inspect")
async def inspect_agent(agent_name: str) -> dict:
    return AgentManager().inspect_agent(agent_name)


@router.post("/agents/{agent_name}/revise")
async def revise_agent(agent_name: str) -> dict:
    return AgentManager().revise_agent(agent_name)


@router.post("/agents/{agent_name}/update")
async def update_agent(agent_name: str, payload: AgentUpdateRequest | None = None) -> dict:
    return await AgentManager().update_agent(agent_name, request=(payload or AgentUpdateRequest()).request)


@router.post("/agents/{agent_name}/rename")
async def rename_agent(agent_name: str, payload: AgentRenameRequest) -> dict:
    return AgentManager().rename_agent(agent_name, new_name=payload.new_name)


@router.post("/agents/{agent_name}/delete")
async def delete_agent(agent_name: str) -> dict:
    return AgentManager().delete_agent(agent_name)


@router.post("/agents/{agent_name}/rollback")
async def rollback_agent(agent_name: str, payload: AgentRollbackRequest | None = None) -> dict:
    return AgentManager().rollback_agent(agent_name, backup_path=(payload or AgentRollbackRequest()).backup_path)


def _build_query_from_proposal(proposal: dict) -> str:
    agent_name = str(proposal.get("agent_name") or "").strip()
    goal = str(proposal.get("goal") or proposal.get("purpose") or "").strip()
    purpose = str(proposal.get("purpose") or "").strip()

    parts = []
    if agent_name:
        parts.append(f"Créer un agent nommé {agent_name}.")
    if goal:
        parts.append(goal)
    if purpose and purpose != goal:
        parts.append(purpose)
    return " ".join(parts).strip() or f"Créer un agent nommé {agent_name}"


def _codex_prompt_from_proposal(proposal: dict) -> str:
    agent_name = str(proposal.get("agent_name") or "").strip()
    goal = str(proposal.get("goal") or proposal.get("purpose") or "").strip()
    purpose = str(proposal.get("purpose") or "").strip()
    capabilities = ", ".join(str(item) for item in proposal.get("required_capabilities") or [])
    proposed_files = "\n".join(f"- {path}" for path in proposal.get("proposed_files") or [])
    tests_to_create = "\n".join(f"- {path}" for path in proposal.get("tests_to_create") or [])

    return "\n".join(
        [
            "Mission Agent Creator Codex, mode expérimental opt-in.",
            "",
            "Contraintes strictes :",
            "- Ne fais aucun commit.",
            "- Ne fais aucun push.",
            "- Ne crée pas de nouveau Registry, Runtime ou CodexRunner.",
            "- Ne modifie pas Evolution.",
            "- Conserve l'approbation humaine comme prérequis.",
            "- Respecte les chemins proposés ou explique dans les tests pourquoi un chemin diffère.",
            "",
            f"Agent demandé : {agent_name}",
            f"Objectif : {goal}",
            f"Purpose : {purpose}",
            f"Capabilities : {capabilities or 'custom_agent_capability'}",
            "",
            "Fichiers proposés :",
            proposed_files or "- workspace/agents/<agent>.py",
            "",
            "Tests à créer :",
            tests_to_create or "- tests/test_<agent>.py",
            "",
            "Critères de réussite :",
            "- L'agent est implémenté de façon déterministe et testable.",
            "- Les tests pytest passent.",
            "- Le runtime existant pourra être rechargé après validation.",
        ]
    )


def _codex_workspace_agent_path(creator: AgentCreator, proposal: dict) -> Path:
    agent_name = str(proposal.get("agent_name") or "").strip()
    return creator.project_root / "workspace" / "agents" / f"{agent_name}.py"


def _existing_codex_created_files(proposal: dict, project_root: Path) -> list[str]:
    candidates = []
    agent_name = str(proposal.get("agent_name") or "").strip()
    if agent_name:
        candidates.append(f"workspace/agents/{agent_name}.py")
    candidates.extend(str(path) for path in proposal.get("proposed_files") or [])
    candidates.extend(str(path) for path in proposal.get("tests_to_create") or [])

    created_files: list[str] = []
    for candidate in candidates:
        path = project_root / candidate
        if path.exists():
            created_files = _append_unique(created_files, _relative_to_root(path, project_root))
    return created_files


def _runtime_generated_dir(runtime) -> Path | None:
    registry = getattr(runtime, "registry", None)
    generated_dir = getattr(registry, "generated_dir", None)
    return Path(generated_dir) if generated_dir is not None else None


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _append_unique(items: list[str], item: str) -> list[str]:
    if item not in items:
        return [*items, item]
    return items


def _build_errors(build_result: dict) -> list[str]:
    errors: list[str] = []
    if build_result.get("status") != "completed":
        project = build_result.get("project") or {}
        error = (
            build_result.get("error")
            or project.get("error")
            or build_result.get("response")
            or "agent_build_failed"
        )
        errors.append(str(error))
    return errors
