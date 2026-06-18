from __future__ import annotations

from typing import Any

from modules.cognitive.critic_engine import get_critic_engine
from agents.factory.agent_creator import AgentCreator
from goal.planning.executor import PlanExecutor
from goal.planning.storage import PlanStorage


AGENT_CREATOR_ACTIONS = {
    "analyze_agents",
    "define_agent",
    "create_skeleton",
    "check_integration",
}


class TaskExecutor:
    def __init__(
        self,
        plan_executor: PlanExecutor | None = None,
        storage: PlanStorage | None = None,
        agent_creator: AgentCreator | None = None,
    ) -> None:
        self.plan_executor = plan_executor or PlanExecutor()
        self.storage = storage or PlanStorage()
        self.agent_creator = agent_creator or AgentCreator()
        if agent_creator is not None:
            self.plan_executor.agent_creator = agent_creator

    def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        action = task.get("action")
        agent = task.get("agent")

        try:
            if action in AGENT_CREATOR_ACTIONS or agent == "agent_creator":
                return self._execute_agent_creator_action(task)

            if action == "analyze_goal":
                return {
                    "status": "success",
                    "agent": agent,
                    "action": action,
                    "summary": "Objectif analyse.",
                    "goal": task.get("goal"),
                }

            if action == "decompose_goal":
                return {
                    "status": "success",
                    "agent": agent,
                    "action": action,
                    "summary": "Objectif decompose en etapes via Planner.",
                    "goal": task.get("goal"),
                }

            if action == "evaluate_plan":
                critic = get_critic_engine()
                plan_id = task.get("plan_id")
                plan = self.storage.get(str(plan_id)) if plan_id else None

                if not plan:
                    plan = {
                        "id": task.get("plan_id"),
                        "goal": task.get("goal"),
                        "steps": [],
                        "approved": False,
                    }

                risk = critic.evaluate_plan(plan)

                if plan_id and plan:
                    plan["risk"] = risk
                    self.storage.update(plan)

                return {
                    "status": "success",
                    "agent": agent,
                    "action": action,
                    "summary": "Risque evalue par CriticEngine sur le vrai plan.",
                    "plan_id": plan_id,
                    "risk": risk,
                }

            return {
                "status": "skipped",
                "agent": agent,
                "action": action,
                "summary": "Action non implementee dans TaskExecutor V1.",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "agent": agent,
                "action": action,
                "summary": "Erreur pendant l'execution de la tache.",
                "error": str(exc),
            }

    def _execute_agent_creator_action(self, task: dict[str, Any]) -> dict[str, Any]:
        action = str(task.get("action") or "")
        plan = self._plan_for_task(task)
        goal = str(plan.get("goal") or task.get("goal") or "")

        if action in AGENT_CREATOR_ACTIONS:
            step = {
                "title": task.get("title"),
                "description": task.get("description"),
                "agent": task.get("agent"),
                "action": action,
            }
            result = self.plan_executor._execute_step(step, plan)
            proposal = self._extract_agent_proposal(result)
            if proposal:
                self._record_agent_proposal(plan, proposal, result.get("agent_path"))

            if result.get("status") == "skipped":
                return {
                    "status": "skipped",
                    "agent": task.get("agent"),
                    "action": action,
                    "summary": result.get("reason") or "Action agent_creator ignoree.",
                    "result": result,
                    "plan_id": task.get("plan_id"),
                }

            return {
                "status": "success",
                "agent": task.get("agent"),
                "action": action,
                "summary": self._agent_creator_summary(action, result),
                "result": result,
                "plan_id": task.get("plan_id"),
                "agent_creator_called": bool(proposal),
                "agent_request_id": (proposal or {}).get("agent_request_id"),
                "agent_creation_proposal": proposal,
                "proposal": proposal,
                "agent_path": result.get("agent_path"),
                "draft_created": result.get("draft_created", False),
                "draft_only": result.get("draft_only", False),
                "applied_to_core": False,
                "code_executed": False,
            }

        if action == "check_integration":
            proposal = plan.get("agent_creation_proposal") or {}
            return {
                "status": "success",
                "agent": task.get("agent"),
                "action": action,
                "summary": "Integration verifiee sans modification du core.",
                "result": {
                    "proposal_status": proposal.get("status", "pending_human_validation"),
                    "applied_to_core": False,
                    "code_execution_allowed": False,
                },
                "plan_id": task.get("plan_id"),
            }

        return {
            "status": "skipped",
            "agent": task.get("agent"),
            "action": action,
            "summary": "Action agent_creator ignoree.",
            "plan_id": task.get("plan_id"),
        }

    def _plan_for_task(self, task: dict[str, Any]) -> dict[str, Any]:
        plan_id = task.get("plan_id")
        plan = self.storage.get(str(plan_id)) if plan_id else None
        if plan:
            return plan

        return {
            "id": plan_id,
            "goal": task.get("goal"),
            "approved": True,
            "steps": [
                {
                    "title": task.get("title"),
                    "description": task.get("description"),
                    "agent": task.get("agent"),
                    "action": task.get("action"),
                }
            ],
        }

    def _record_agent_proposal(
        self,
        plan: dict[str, Any],
        proposal: dict[str, Any],
        agent_path: str | None = None,
    ) -> None:
        if not plan.get("id"):
            return

        plan["agent_creator_called"] = True
        plan["agent_request_id"] = proposal.get("agent_request_id")
        plan["agent_creation_proposal"] = proposal
        plan["agent_proposal_status"] = proposal.get("status")
        plan["applied_to_core"] = False
        if agent_path:
            plan["agent_path"] = agent_path
            plan["agent_state"] = "draft_only"
            plan["agent_draft"] = {"path": agent_path, "state": "draft_only"}
        self.storage.update(plan)

    def _extract_agent_proposal(self, result: dict[str, Any]) -> dict[str, Any] | None:
        proposal = result.get("agent_creation_proposal") or result.get("proposal")
        return proposal if isinstance(proposal, dict) else None

    def _agent_creator_summary(self, action: str, result: dict[str, Any]) -> str:
        if action == "create_skeleton" and result.get("agent_path"):
            return f"Brouillon agent cree : {result.get('agent_path')}"
        if action == "define_agent" and result.get("proposal_created"):
            return "Role et proposition d'agent prepares."
        if action == "analyze_agents":
            return "Agents existants analyses."
        if action == "check_integration":
            return "Integration verifiee sans modification du core."
        return "Action agent_creator executee."


def get_task_executor() -> TaskExecutor:
    return TaskExecutor()
