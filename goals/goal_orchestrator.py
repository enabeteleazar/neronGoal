from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from modules.cognitive.critic_engine import get_critic_engine
from agents.factory.agent_creator import AgentCreator
from agents.factory.build_orchestrator import AgentBuildOrchestrator
from modules.events.event import Event
from modules.events.event_bus import event_bus
from goal.goals.execution_engine import (
    GoalExecutionEngine,
    get_goal_execution_engine,
)
from goal.goals.goal_manager import get_goal_manager
from goal.planning import AutonomousPlanner
from goal.planning.storage import PlanStorage
from goal.projects.manager import get_project_manager
from goal.system.task_executor import get_task_executor
from goal.system.task_manager import get_task_manager

Notifier = Callable[[str, str], Awaitable[None]]

PLAN_TERMINAL_STATUSES = {
    "plan_finished",
    "partial",
    "refused",
    "blocked_by_risk",
    "failed",
    "superseded",
    "archived",
}

SENSITIVE_ACTIONS = {
    "delete_file",
    "remove_file",
    "rm",
    "unlink",
    "modify_system_config",
    "modify_systemd",
    "restart_systemd",
    "write_secret",
    "read_secret",
    "modify_secret",
    "modify_security",
    "modify_core",
    "apply_destructive_change",
    "destructive_action",
}

SENSITIVE_KEYWORDS = {
    "supprimer",
    "suppression",
    "delete",
    "remove",
    "destructif",
    "destructive",
    "systemd",
    "service systemd",
    "configuration système",
    "system config",
    "secret",
    "secrets",
    "token",
    "tokens",
    "clé ssh",
    "ssh key",
    "api key",
    "sécurité",
    "security",
    "core critique",
    "chmod",
    "rm -rf",
    "shell",
    "exécuter du code",
    "execute code",
}


class GoalOrchestrator:
    """
    Orchestrateur semi-autonome du workflow Goal -> Plan -> Tasks -> Risk -> Execution.

    Il centralise la logique jusque-là répartie entre Telegram, Planner et TaskManager,
    tout en conservant les composants existants et les routes publiques.
    """

    def __init__(
        self,
        planner: AutonomousPlanner | None = None,
        storage: PlanStorage | None = None,
        notifier: Notifier | None = None,
        agent_build_orchestrator: AgentBuildOrchestrator | None = None,
        agent_creator: AgentCreator | None = None,
        execution_engine: GoalExecutionEngine | None = None,
    ) -> None:
        self.goal_manager = get_goal_manager()
        self.planner = planner or AutonomousPlanner()
        self.storage = storage or PlanStorage()
        self.task_manager = get_task_manager()
        self.task_executor = get_task_executor()
        self.critic = get_critic_engine()
        self.notifier = notifier
        self.agent_build_orchestrator = agent_build_orchestrator or AgentBuildOrchestrator()
        self.agent_creator = agent_creator or AgentCreator()
        self.execution_engine = execution_engine or get_goal_execution_engine()

    def queue_goal(self, objective: str, source: str = "system") -> dict[str, Any]:
        title = objective.strip()
        if not title:
            raise ValueError("Objectif vide.")
        goal = self.goal_manager.create_goal(
            title=title,
            priority="high" if source == "telegram" else "medium",
            source=source,
            metadata={"orchestrated": True, "asynchronous": True},
            deduplicate=False,
        )
        queued = self.goal_manager.update_status(
            str(goal["id"]),
            "queued",
            current_step="queued",
        ) or goal
        self._execution_engine().enqueue_goal(
            str(goal["id"]),
            title,
            source,
            dict(queued.get("metadata") or {}),
        )
        return queued

    async def run_goal(
        self,
        objective: str,
        source: str = "system",
        goal_id: str | None = None,
    ) -> dict[str, Any]:
        title = objective.strip()
        if not title:
            return {"status": "invalid", "message": "Objectif vide."}

        goal = self.goal_manager.get_goal(goal_id) if goal_id else None
        if goal is None:
            goal = self.goal_manager.create_goal(
                title=title,
                priority="high" if source == "telegram" else "medium",
                source=source,
                metadata={"orchestrated": True},
            )
        execution_engine = self._execution_engine()
        if execution_engine.get_goal_status(str(goal["id"])) is None:
            execution_engine.enqueue_goal(
                str(goal["id"]),
                title,
                source,
                dict(goal.get("metadata") or {}),
            )
        execution_engine.start_goal(str(goal["id"]))
        execution_engine.mark_step_started(str(goal["id"]), "planning")
        self._update_goal_status(
            str(goal["id"]),
            "active",
            current_step="planning",
        )
        goal = self.goal_manager.get_goal(str(goal["id"])) or goal

        plan = self.planner.create_plan(title).to_dict()
        plan.update(
            {
                "goal_id": goal.get("id"),
                "planner_called": True,
                "source": f"{source}_goal",
                "status": "pending",
                "approved": False,
                "approval_required": False,
                "orchestrated": True,
            }
        )
        goal_metadata = dict(goal.get("metadata") or {})
        if goal_metadata.get("internal_capability_request"):
            plan["internal_capability_request"] = True
            plan["capability_request_id"] = goal_metadata.get("capability_request_id")
            plan["creation_type"] = goal_metadata.get("creation_type")
            plan["capability_original_text"] = goal_metadata.get("capability_original_text")
            plan["required_tools"] = list(goal_metadata.get("required_tools") or [])
            plan["created_tools"] = list(goal_metadata.get("created_tools") or [])
            plan["tool_creation_status"] = goal_metadata.get(
                "tool_creation_status",
                "not_required",
            )
        execution_engine.mark_step_completed(
            str(goal["id"]),
            "planning",
            {"plan_id": plan.get("id")},
        )
        self.goal_manager.update_progress(str(goal["id"]), 0.1)

        created_tasks = self.task_manager.create_tasks_from_plan(plan)
        plan["tasks_generated"] = True
        plan["generated_task_ids"] = [task.get("id") for task in created_tasks]
        self.goal_manager.update_progress(str(goal["id"]), 0.2)

        risk = self.critic.evaluate_plan(plan)
        sensitive = self._detect_sensitive_action(plan)
        if sensitive["detected"]:
            risk = {
                **risk,
                "risk_score": max(int(risk.get("risk_score", 0)), 90),
                "risk_level": "critical",
                "execution_allowed": False,
                "sensitive_action_detected": True,
                "sensitive_reasons": sensitive["reasons"],
            }
        plan["risk"] = risk

        decision = self._decide(risk, sensitive["detected"])
        plan["decision"] = decision
        plan["decision_at"] = self._now()

        if decision == "blocked":
            plan["status"] = "blocked_by_risk"
            plan["approval_required"] = False
            plan["error"] = "Exécution bloquée par le CriticEngine."
            self.storage.save(plan)
            self._update_goal_status(
                str(goal["id"]),
                "failed",
                current_step="blocked_by_risk",
                error=str(plan["error"]),
            )
            execution_engine.mark_step_failed(
                str(goal["id"]),
                "planning",
                str(plan["error"]),
            )
            await self._notify_blocked(plan)
            return self._goal_response(decision, goal, plan, created_tasks)

        if decision == "approval_required":
            plan["status"] = "approval_required"
            plan["approval_required"] = True
            self.storage.save(plan)
            await self._notify_approval_required(plan)
            return self._goal_response(decision, goal, plan, created_tasks)

        plan["approved"] = True
        plan["approval_required"] = False
        plan["approved_by"] = "risk_policy"
        plan["approved_at"] = self._now()
        plan["status"] = "approved"
        self.storage.save(plan)

        if self._is_agent_creation_plan(plan):
            execution = await self.execute_agent_build_plan(plan, approved_by="risk_policy")
            return self._goal_response(execution["status"], goal, execution["plan"], created_tasks)

        execution = await self.execute_plan(plan, approved_by="risk_policy")
        return self._goal_response(execution["status"], goal, execution["plan"], created_tasks)

    async def execute_agent_build_plan(
        self,
        plan: dict[str, Any],
        approved_by: str = "system",
    ) -> dict[str, Any]:
        plan_id = str(plan.get("id") or "")
        if not plan_id:
            return {"status": "failed", "plan": plan, "error": "Plan sans identifiant."}

        if plan.get("approved") is not True:
            plan["status"] = "approval_required"
            plan["approval_required"] = True
            self.storage.update(plan)
            return {"status": "approval_required", "plan": plan}

        related_tasks = self._tasks_for_plan(plan_id)
        if not related_tasks:
            related_tasks = self.task_manager.create_tasks_from_plan(plan)
            plan["tasks_generated"] = True
            plan["generated_task_ids"] = [task.get("id") for task in related_tasks]

        plan["status"] = "running"
        plan["execution_started_at"] = self._now()
        plan["executed_by"] = approved_by
        plan["agent_build_orchestrator_called"] = True
        plan["current_step"] = "agent_creator"
        if plan.get("goal_id"):
            self.goal_manager.update_progress(str(plan.get("goal_id")), 0.3)
        self.storage.update(plan)
        await self._notify_execution_started(plan)

        task_executor = getattr(self, "task_executor", None)
        creator = (
            getattr(task_executor, "agent_creator", None)
            or getattr(self, "agent_creator", None)
        )
        if creator is not None:
            proposal = creator.request_agent_creation(
                goal=str(plan.get("goal") or ""),
                plan=plan,
            )
        else:
            proposal = {
                "agent_request_id": f"legacy-{plan_id}",
                "agent_name": None,
                "goal": str(plan.get("goal") or ""),
                "required_capabilities": [],
                "required_tools": list(plan.get("required_tools") or []),
                "status": "pending_human_validation",
                "human_validation_required": True,
                "code_execution_allowed": False,
                "applied_to_core": False,
                "created_from_goal_id": plan.get("goal_id"),
                "created_from_plan_id": plan_id,
            }
        plan["agent_creator_called"] = True
        plan["agent_creation_proposal"] = proposal
        plan["agent_request_id"] = proposal.get("agent_request_id")
        plan["agent_proposal_status"] = proposal.get("status")
        plan["current_step"] = "agent_build"
        self.storage.update(plan)
        await self._publish_flow_event(
            "agent_creator.proposal_created",
            {
                "goal_id": plan.get("goal_id"),
                "plan_id": plan.get("id"),
                "agent_request_id": proposal.get("agent_request_id"),
                "agent_name": proposal.get("agent_name"),
                "status": proposal.get("status"),
            },
        )

        source_channel = str(plan.get("source") or "api_goal").removesuffix("_goal")
        build_result = await self._run_agent_build(
            plan,
            approved_by=approved_by,
            source_channel=source_channel,
            proposal=proposal,
        )
        plan["agent_build_result"] = build_result
        plan["agent_build_status"] = build_result.get("status")
        plan["agent_creator_called"] = True
        self._attach_build_result(plan, build_result)
        if creator is not None and hasattr(creator, "update_proposal"):
            updated_proposal = creator.update_proposal(
                str(plan.get("agent_request_id") or ""),
                plan.get("agent_creation_proposal") or {},
            )
            if updated_proposal:
                plan["agent_creation_proposal"] = updated_proposal
                plan["agent_proposal_status"] = updated_proposal.get("status")

        if build_result.get("status") != "completed":
            for task in related_tasks:
                self.task_manager.update_task(
                    str(task.get("id")),
                    {
                        "status": "failed",
                        "progress": 100,
                        "result": build_result,
                        "error": build_result.get("response") or "agent_build_failed",
                    },
                )
            plan["status"] = "refused" if build_result.get("status") == "refused" else "failed"
            plan["tasks_completed"] = False
            plan["tasks_completed_at"] = self._now()
            plan["error"] = build_result.get("response") or "Création d'agent échouée."
            plan["execution_summary"] = {
                "completed": 0,
                "skipped": 0,
                "failed": len(related_tasks),
                "total": len(related_tasks),
            }
            self.storage.update(plan)
            if plan.get("goal_id"):
                project = build_result.get("project") or {}
                failed_step = str(
                    project.get("current_step")
                    or plan.get("current_step")
                    or "failed"
                )
                self._update_goal_status(
                    str(plan.get("goal_id")),
                    "failed",
                    current_step=failed_step,
                    error=str(plan.get("error") or "agent_build_failed"),
                )
                execution_engine = self._execution_engine_or_none()
                current_run = (
                    execution_engine.get_goal_status(str(plan.get("goal_id")))
                    if execution_engine is not None
                    else {}
                ) or {}
                if execution_engine is not None and current_run.get("status") != "failed":
                    execution_engine.mark_step_failed(
                        str(plan.get("goal_id")),
                        failed_step,
                        str(plan.get("error") or "agent_build_failed"),
                    )
            await self._notify_execution_failed(plan)
            return {"status": plan["status"], "plan": plan, "error": plan.get("error")}

        completed_ids: list[str] = []
        for task in related_tasks:
            updated = self.task_manager.update_task(
                str(task.get("id")),
                {
                    "status": "completed",
                    "progress": 100,
                    "completed_at": self.task_manager._now(),
                    "result": build_result,
                    "error": None,
                },
            )
            completed_ids.append(str(task.get("id")))
            self._update_plan_step_from_task(plan, updated or task)

        plan["completed_task_ids"] = completed_ids
        plan["skipped_task_ids"] = []
        plan["failed_task_ids"] = []
        plan["tasks_completed"] = True
        plan["tasks_completed_at"] = self._now()
        plan["execution_summary"] = {
            "completed": len(completed_ids),
            "skipped": 0,
            "failed": 0,
            "total": len(related_tasks),
        }
        plan["task_counts"] = dict(plan["execution_summary"])
        plan["status"] = "plan_finished"
        plan["current_step"] = "completed"
        plan["finished_at"] = self._now()
        plan["error"] = None
        self.storage.update(plan)
        self._complete_goal(plan)
        await self._notify_execution_finished(plan)
        return {"status": "plan_finished", "plan": plan}

    async def execute_approved_plan(self, plan_id: str, approved_by: str = "telegram") -> dict[str, Any]:
        plan = self.find_plan(plan_id)
        if not plan:
            return {"status": "not_found", "error": "Plan introuvable."}

        if plan.get("status") in PLAN_TERMINAL_STATUSES:
            return {"status": "not_executable", "plan": plan, "error": "Plan déjà finalisé."}

        plan["approved"] = True
        plan["approval_required"] = False
        plan["approved_by"] = approved_by
        plan["approved_at"] = self._now()
        plan["status"] = "approved"
        plan["error"] = None

        risk = self.critic.evaluate_plan(plan)
        sensitive = self._detect_sensitive_action(plan)
        if sensitive["detected"]:
            risk = {
                **risk,
                "risk_score": max(int(risk.get("risk_score", 0)), 90),
                "risk_level": "critical",
                "execution_allowed": False,
                "sensitive_action_detected": True,
                "sensitive_reasons": sensitive["reasons"],
            }
        plan["risk"] = risk

        if self._decide(risk, sensitive["detected"]) == "blocked":
            plan["status"] = "blocked_by_risk"
            plan["error"] = "Exécution bloquée par le CriticEngine."
            self.storage.update(plan)
            await self._notify_blocked(plan)
            return {"status": "blocked", "plan": plan, "risk": risk}

        self.storage.update(plan)
        return await self.execute_plan(plan, approved_by=approved_by)

    async def execute_plan(self, plan: dict[str, Any], approved_by: str = "system") -> dict[str, Any]:
        plan_id = str(plan.get("id") or "")
        if not plan_id:
            return {"status": "failed", "plan": plan, "error": "Plan sans identifiant."}

        if plan.get("approved") is not True:
            plan["status"] = "approval_required"
            plan["approval_required"] = True
            self.storage.update(plan)
            return {"status": "approval_required", "plan": plan}

        related_tasks = self._tasks_for_plan(plan_id)
        if not related_tasks:
            related_tasks = self.task_manager.create_tasks_from_plan(plan)
            plan["tasks_generated"] = True
            plan["generated_task_ids"] = [task.get("id") for task in related_tasks]

        plan["status"] = "running"
        plan["execution_started_at"] = self._now()
        plan["executed_by"] = approved_by
        self.storage.update(plan)
        await self._notify_execution_started(plan)

        total = len(related_tasks)
        completed_ids: list[str] = []
        skipped_ids: list[str] = []
        failed_ids: list[str] = []
        agent_path: str | None = None

        for index, task in enumerate(related_tasks, start=1):
            if task.get("status") == "completed":
                completed_ids.append(str(task.get("id")))
                self._update_plan_step_from_task(plan, task)
                existing_result = task.get("result") or {}
                agent_path = agent_path or existing_result.get("agent_path")
                self._attach_agent_proposal_from_result(plan, existing_result)
                continue

            if task.get("status") == "skipped":
                skipped_ids.append(str(task.get("id")))
                self._update_plan_step_from_task(plan, task)
                continue

            self.task_manager.update_task(str(task["id"]), {"status": "in_progress"})
            current_task = self.task_manager.get_task(str(task["id"])) or task

            try:
                result = self.task_executor.execute(current_task)
                if result.get("status") == "success":
                    agent_path = agent_path or result.get("agent_path")
                    proposal = self._extract_agent_proposal(result)
                    self._attach_agent_proposal_from_result(plan, result)
                    proposal_id = (proposal or {}).get("agent_request_id")
                    if proposal and plan.get("agent_creator_event_published_for") != proposal_id:
                        await self._publish_flow_event(
                            "agent_creator.proposal_created",
                            {
                                "goal_id": plan.get("goal_id"),
                                "plan_id": plan.get("id"),
                                "agent_request_id": proposal_id,
                                "agent_name": proposal.get("agent_name"),
                                "status": proposal.get("status"),
                            },
                        )
                        plan["agent_creator_event_published_for"] = proposal_id
                    updated = self.task_manager.update_task(
                        str(task["id"]),
                        {
                            "status": "completed",
                            "progress": 100,
                            "completed_at": self.task_manager._now(),
                            "result": result,
                            "error": None,
                        },
                    )
                    completed_ids.append(str(task.get("id")))
                    self._update_plan_step_from_task(plan, updated or current_task)
                    await self._notify_task_done(plan, updated or current_task, index, total)
                elif result.get("status") == "skipped":
                    updated = self.task_manager.update_task(
                        str(task["id"]),
                        {"status": "skipped", "result": result, "progress": 100},
                    )
                    skipped_ids.append(str(task.get("id")))
                    self._update_plan_step_from_task(plan, updated or current_task)
                    await self._notify_task_done(plan, updated or current_task, index, total, skipped=True)
                else:
                    error = result.get("error") or result.get("summary") or "Action échouée."
                    failed = self.task_manager.fail_task(str(task["id"]), str(error))
                    failed_ids.append(str(task.get("id")))
                    self._update_plan_step_from_task(plan, failed or current_task)
                    await self._notify_task_failed(plan, failed or current_task, index, total, str(error))
                    plan["status"] = "failed"
                    plan["error"] = str(error)
                    plan["failed_task_ids"] = failed_ids
                    self._attach_execution_summary(plan, related_tasks, completed_ids, skipped_ids, failed_ids)
                    self.storage.update(plan)
                    await self._notify_execution_failed(plan)
                    return {"status": "failed", "plan": plan, "error": str(error)}
            except Exception as exc:
                failed = self.task_manager.fail_task(str(task["id"]), str(exc))
                failed_ids.append(str(task.get("id")))
                self._update_plan_step_from_task(plan, failed or current_task)
                await self._notify_task_failed(plan, failed or current_task, index, total, str(exc))
                plan["status"] = "failed"
                plan["error"] = str(exc)
                plan["failed_task_ids"] = failed_ids
                self._attach_execution_summary(plan, related_tasks, completed_ids, skipped_ids, failed_ids)
                self.storage.update(plan)
                await self._notify_execution_failed(plan)
                return {"status": "failed", "plan": plan, "error": str(exc)}

        plan["completed_task_ids"] = completed_ids
        plan["skipped_task_ids"] = skipped_ids
        plan["failed_task_ids"] = failed_ids
        plan["task_counts"] = {
            "total": total,
            "completed": len(completed_ids),
            "skipped": len(skipped_ids),
            "failed": len(failed_ids),
        }
        if agent_path:
            plan["agent_path"] = agent_path
            plan["agent_draft"] = {"path": agent_path, "state": "draft_only"}

        creation_actions = {"analyze_agents", "define_agent", "create_skeleton", "check_integration"}
        creation_task_ids = [
            str(task.get("id"))
            for task in related_tasks
            if task.get("action") in creation_actions
        ]
        create_skeleton_tasks = [
            task for task in related_tasks if task.get("action") == "create_skeleton"
        ]
        creation_goal = bool(creation_task_ids)
        creation_skipped = creation_goal and all(
            task_id in skipped_ids for task_id in creation_task_ids
        )
        missing_draft = creation_goal and create_skeleton_tasks and not plan.get("agent_path")

        if failed_ids or creation_skipped or missing_draft or (total and not completed_ids and skipped_ids):
            plan["tasks_completed"] = False
            plan["tasks_completed_at"] = self._now()
            plan["status"] = "failed" if failed_ids or creation_skipped or missing_draft else "partial"
            plan["error"] = self._final_error(plan, creation_skipped, missing_draft)
            self.storage.update(plan)
            await self._notify_execution_failed(plan)
            return {"status": plan["status"], "plan": plan, "error": plan.get("error")}

        plan["tasks_completed"] = True
        plan["tasks_completed_at"] = self._now()
        plan["status"] = "tasks_completed"
        self._attach_execution_summary(plan, related_tasks, completed_ids, skipped_ids, failed_ids)
        self.storage.update(plan)

        if self._requires_agent_draft(plan) and not plan.get("agent_path"):
            plan["status"] = "failed"
            plan["error"] = "Brouillon agent non créé."
            self.storage.update(plan)
            await self._notify_execution_failed(plan)
            return {"status": "failed", "plan": plan, "error": plan["error"]}

        if not completed_ids:
            plan["status"] = "failed"
            plan["error"] = "Aucune tâche exécutée."
            self.storage.update(plan)
            await self._notify_execution_failed(plan)
            return {"status": "failed", "plan": plan, "error": plan["error"]}

        if skipped_ids:
            plan["status"] = "partial"
            plan["partial_at"] = self._now()
            plan["error"] = "Certaines tâches ont été ignorées."
            self.storage.update(plan)
            await self._notify_execution_partial(plan)
            return {"status": "partial", "plan": plan, "error": plan["error"]}

        plan["status"] = "plan_finished"
        plan["finished_at"] = self._now()
        plan["error"] = None
        self.storage.update(plan)
        self._complete_goal(plan)
        await self._notify_execution_finished(plan)
        return {"status": "plan_finished", "plan": plan}

    def _final_error(self, plan: dict[str, Any], creation_skipped: bool, missing_draft: bool) -> str:
        if plan.get("failed_task_ids"):
            return "Une ou plusieurs tâches ont échoué."
        if creation_skipped:
            return "Objectif de création d'agent non exécuté : toutes les tâches importantes ont été ignorées."
        if missing_draft:
            return "Objectif de création d'agent incomplet : aucun brouillon n'a été créé."
        return "Aucune tâche exécutable n'a été terminée."

    def find_plan(self, plan_id_or_prefix: str) -> dict[str, Any] | None:
        wanted = plan_id_or_prefix.strip()
        if not wanted:
            return None

        exact = self.storage.get(wanted)
        if exact:
            return exact

        for candidate in self.storage.history(limit=500):
            plan_id = str(candidate.get("id") or "")
            if plan_id.startswith(wanted):
                return candidate

        return None

    def sync_plan_task_status(self, plan_id: str) -> dict[str, Any] | None:
        plan = self.find_plan(plan_id)
        if not plan:
            return None

        tasks = self._tasks_for_plan(str(plan.get("id")))
        if not tasks:
            return plan

        completed_ids = [str(task.get("id")) for task in tasks if task.get("status") == "completed"]
        skipped_ids = [str(task.get("id")) for task in tasks if task.get("status") == "skipped"]
        failed_ids = [str(task.get("id")) for task in tasks if task.get("status") == "failed"]

        for task in tasks:
            self._update_plan_step_from_task(plan, task)

        self._attach_execution_summary(plan, tasks, completed_ids, skipped_ids, failed_ids)

        statuses = {task.get("status") for task in tasks}
        if failed_ids:
            plan["status"] = "failed"
        elif statuses <= {"completed", "skipped"}:
            completed = [task for task in tasks if task.get("status") == "completed"]
            skipped = [task for task in tasks if task.get("status") == "skipped"]
            creation_actions = {"analyze_agents", "define_agent", "create_skeleton", "check_integration"}
            creation_tasks = [task for task in tasks if task.get("action") in creation_actions]
            all_creation_skipped = bool(creation_tasks) and all(
                task.get("status") == "skipped" for task in creation_tasks
            )
            if all_creation_skipped or (skipped and not completed):
                plan["status"] = "failed" if all_creation_skipped else "partial"
                plan["tasks_completed"] = False
                plan["error"] = "Aucune tâche exécutable n'a été terminée."
            else:
                plan["status"] = "tasks_completed"
                plan["tasks_completed"] = True
            plan["completed_task_ids"] = [task.get("id") for task in completed]
            plan["skipped_task_ids"] = [task.get("id") for task in skipped]
        elif "in_progress" in statuses:
            plan["status"] = "running"
        elif plan.get("status") not in {"approval_required", "approved", "blocked_by_risk"}:
            plan["status"] = "pending"

        self.storage.update(plan)
        return plan

    def _update_plan_step_from_task(self, plan: dict[str, Any], task: dict[str, Any]) -> None:
        task_action = task.get("action")
        task_title = task.get("title")

        for step in plan.get("steps", []):
            if task_action and step.get("action") != task_action:
                continue

            if not task_action and task_title and step.get("title") != task_title:
                continue

            status = task.get("status")
            if status in {"completed", "skipped", "failed", "in_progress"}:
                step["status"] = status

            if "result" in task:
                step["result"] = task.get("result")

            if task.get("error"):
                step["error"] = task.get("error")
            elif status in {"completed", "skipped"}:
                step["error"] = None

            return

    def _attach_execution_summary(
        self,
        plan: dict[str, Any],
        tasks: list[dict[str, Any]],
        completed_ids: list[str],
        skipped_ids: list[str],
        failed_ids: list[str],
    ) -> None:
        plan["completed_task_ids"] = completed_ids
        plan["skipped_task_ids"] = skipped_ids
        plan["failed_task_ids"] = failed_ids
        plan["execution_summary"] = {
            "completed": len(completed_ids),
            "skipped": len(skipped_ids),
            "failed": len(failed_ids),
            "total": len(tasks),
        }

        agent_path = self._find_agent_path(tasks)
        if agent_path:
            plan["agent_path"] = agent_path
            plan["agent_state"] = "draft_only"

        for task in tasks:
            self._attach_agent_proposal_from_result(plan, task.get("result") or {})

    def _find_agent_path(self, tasks: list[dict[str, Any]]) -> str | None:
        for task in tasks:
            result = task.get("result") or {}
            path = result.get("agent_path")

            if not path and isinstance(result.get("result"), dict):
                path = result["result"].get("path")

            if path:
                return self._project_relative_path(str(path))

        return None

    def _attach_agent_proposal_from_result(
        self,
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        proposal = self._extract_agent_proposal(result)
        if not proposal:
            return

        plan["agent_creator_called"] = True
        plan["agent_creation_proposal"] = proposal
        plan["agent_request_id"] = proposal.get("agent_request_id")
        plan["agent_proposal_status"] = proposal.get("status")
        plan["applied_to_core"] = False

    def _attach_build_result(self, plan: dict[str, Any], build_result: dict[str, Any]) -> None:
        project = build_result.get("project") or {}
        spec = build_result.get("spec") or {}
        registered_record = build_result.get("agent") or {}
        result = project.get("result") or {}
        created_files = list(project.get("created_files") or [])
        agent_name = (
            project.get("registered_agent")
            or result.get("agent")
            or registered_record.get("agent_name")
            or spec.get("name")
        )
        runtime_reload = (
            build_result.get("runtime_reload")
            or result.get("runtime_reload")
            or ((result.get("verification") or {}).get("runtime_reload") if isinstance(result.get("verification"), dict) else None)
        )
        test_results = project.get("test_results") or []
        tests_ok = bool(test_results) and all(item.get("returncode") == 0 for item in test_results)
        agent_path = next(
            (path for path in created_files if str(path).endswith(f"{agent_name}.py") and "core/agents/generated/" in str(path)),
            None,
        )

        if agent_name:
            plan["agent_name"] = agent_name
            plan["registered_agent"] = agent_name
            plan["agent_request_id"] = (
                plan.get("agent_request_id")
                or str(project.get("project_id") or agent_name)
            )
        if agent_path:
            plan["agent_path"] = agent_path
            plan["agent_state"] = "registered"
        plan["created_files"] = created_files
        plan["build_project_id"] = project.get("project_id")
        reused_registered = bool(build_result.get("reused_registered_agent"))
        plan["registry_status"] = project.get("registry_status") or (
            "registered" if reused_registered else None
        )
        plan["validation_status"] = project.get("validation_status") or (
            "previously_validated" if reused_registered else None
        )
        plan["compile_status"] = project.get("compile_status") or (
            "previously_validated" if reused_registered else None
        )
        plan["test_status"] = project.get("test_status") or (
            "previously_validated" if reused_registered else None
        )
        plan["business_validation_status"] = project.get("business_validation_status") or (
            "not_validated" if reused_registered else None
        )
        plan["business_validation_result"] = project.get("business_validation_result")
        plan["governor_status"] = project.get("governor_status") or (
            "not_required" if reused_registered else None
        )
        plan["runtime_status"] = project.get("runtime_status") or (
            "available" if reused_registered and build_result.get("status") == "completed" else None
        )
        plan["runtime_reload"] = runtime_reload
        plan["tests_ok"] = tests_ok
        plan["build_mode"] = build_result.get("build_mode")
        plan["codex_used"] = build_result.get("codex_used")
        plan["codex_ok"] = build_result.get("codex_ok")
        plan["codex_fallback"] = build_result.get("codex_fallback")
        plan["codex_error"] = build_result.get("codex_error")
        plan["applied_to_core"] = build_result.get("status") == "completed" and bool(agent_name)
        plan["human_validation_required"] = False
        plan["agent_creation_proposal"] = {
            **(plan.get("agent_creation_proposal") or {}),
            "agent_request_id": plan.get("agent_request_id"),
            "agent_name": agent_name,
            "goal": spec.get("goal") or plan.get("goal"),
            "required_capabilities": spec.get("capabilities") or [],
            "status": "auto_applied" if build_result.get("status") == "completed" else build_result.get("status"),
            "human_validation_required": False,
            "code_execution_allowed": True,
            "applied_to_core": plan["applied_to_core"],
            "created_from_goal_id": plan.get("goal_id"),
            "created_from_plan_id": plan.get("id"),
            "build_mode": plan.get("build_mode"),
            "codex_used": plan.get("codex_used"),
            "codex_ok": plan.get("codex_ok"),
            "codex_fallback": plan.get("codex_fallback"),
        }
        plan["agent_proposal_status"] = plan["agent_creation_proposal"]["status"]

    def get_goal_status(self, goal_id: str) -> dict[str, Any] | None:
        execution_status = self._execution_engine().get_goal_status(goal_id)
        goal = self.goal_manager.get_goal(goal_id)
        if not goal and not execution_status:
            return None
        goal = goal or {
            "id": goal_id,
            "status": execution_status.get("status"),
            "current_step": execution_status.get("current_step"),
            "progress": float(execution_status.get("progress") or 0) / 100,
            "error": execution_status.get("error"),
        }

        plan = self.storage.find_by_goal_id(goal_id)
        project_manager = getattr(self.agent_build_orchestrator, "project_manager", None)
        if project_manager is None:
            project_manager = get_project_manager()
        project = None
        if plan and plan.get("build_project_id"):
            project = project_manager.get_project(str(plan["build_project_id"]))
        if project is None:
            project = project_manager.find_project_by_tracking(
                goal_id=goal_id,
                plan_id=str((plan or {}).get("id") or "") or None,
            )

        project_metadata = (project or {}).get("metadata") or {}
        project_result = (project or {}).get("result") or {}
        agent_slug = (
            (plan or {}).get("registered_agent")
            or (plan or {}).get("agent_name")
            or (project or {}).get("registered_agent")
            or project_result.get("agent")
            or project_metadata.get("agent_name")
        )
        errors = self._goal_status_errors(goal, plan, project)
        status = self._public_goal_status(goal, plan, project)
        current_step = str(
            (project or {}).get("current_step")
            or (plan or {}).get("current_step")
            or goal.get("current_step")
            or status
        )
        progress = self._goal_status_progress(goal, plan, project)
        business_validation_status = (project or {}).get("business_validation_status")
        if not business_validation_status:
            business_validation_status = (plan or {}).get("business_validation_status")
        if not business_validation_status:
            business_validation_status = (
                "not_validated"
                if status == "completed" and ((project is not None) or (plan or {}).get("build_project_id"))
                else "pending"
            )

        legacy_status = {
            "goal_id": goal_id,
            "status": status,
            "current_step": current_step,
            "progress": progress,
            "plan_id": (plan or {}).get("id"),
            "project_id": (project or {}).get("project_id") or (plan or {}).get("build_project_id"),
            "agent_slug": agent_slug,
            "codex_used": bool(
                (plan or {}).get("codex_used")
                or project_metadata.get("codex_used")
            ),
            "validation_status": (project or {}).get("validation_status") or (plan or {}).get("validation_status") or "pending",
            "compile_status": (project or {}).get("compile_status") or (plan or {}).get("compile_status") or "pending",
            "test_status": (project or {}).get("test_status") or (plan or {}).get("test_status") or "pending",
            "business_validation_status": business_validation_status,
            "business_validation_result": (project or {}).get("business_validation_result") or (plan or {}).get("business_validation_result"),
            "governor_status": (project or {}).get("governor_status") or (plan or {}).get("governor_status") or "pending",
            "registry_status": (project or {}).get("registry_status") or (plan or {}).get("registry_status") or "not_registered",
            "runtime_status": (project or {}).get("runtime_status") or (plan or {}).get("runtime_status") or "pending",
            "error": errors[0] if errors else goal.get("error"),
            "errors": errors,
            "steps": list((project or {}).get("steps") or []),
        }
        goal_metadata = goal.get("metadata") or {}
        tool_metadata_present = any(
            key in source
            for source in (project_metadata, plan or {}, goal_metadata)
            for key in (
                "required_tools",
                "created_tools",
                "tool_creation_status",
            )
        )
        if tool_metadata_present:
            legacy_status.update(
                {
                    "required_tools": list(
                        project_metadata.get("required_tools")
                        or (plan or {}).get("required_tools")
                        or goal_metadata.get("required_tools")
                        or []
                    ),
                    "created_tools": list(
                        project_metadata.get("created_tools")
                        or (plan or {}).get("created_tools")
                        or goal_metadata.get("created_tools")
                        or []
                    ),
                    "tool_creation_status": (
                        project_metadata.get("tool_creation_status")
                        or (plan or {}).get("tool_creation_status")
                        or goal_metadata.get("tool_creation_status")
                        or "not_required"
                    ),
                }
            )
        if not execution_status:
            return legacy_status
        return {
            **legacy_status,
            **execution_status,
            "codex_used": legacy_status["codex_used"],
            "validation_status": legacy_status["validation_status"],
            "compile_status": legacy_status["compile_status"],
            "test_status": legacy_status["test_status"],
            "business_validation_status": legacy_status["business_validation_status"],
            "business_validation_result": legacy_status["business_validation_result"],
            "governor_status": legacy_status["governor_status"],
            "registry_status": legacy_status["registry_status"],
            "runtime_status": legacy_status["runtime_status"],
            "errors": legacy_status["errors"],
            "steps": legacy_status["steps"],
        }

    def _public_goal_status(
        self,
        goal: dict[str, Any],
        plan: dict[str, Any] | None,
        project: dict[str, Any] | None,
    ) -> str:
        goal_status = str(goal.get("status") or "")
        plan_status = str((plan or {}).get("status") or "")
        project_status = str((project or {}).get("status") or "")
        if goal_status == "completed" or plan_status == "plan_finished":
            return "completed"
        if goal_status == "failed" or plan_status in {"failed", "refused", "blocked_by_risk"}:
            return "failed"
        if project_status in {"completed", "failed"}:
            return project_status
        return plan_status or goal_status or "unknown"

    def _goal_status_errors(
        self,
        goal: dict[str, Any],
        plan: dict[str, Any] | None,
        project: dict[str, Any] | None,
    ) -> list[str]:
        errors: list[str] = []
        for value in (
            goal.get("error"),
            (plan or {}).get("error"),
            (project or {}).get("error"),
        ):
            if value and str(value) not in errors:
                errors.append(str(value))
        for step in (project or {}).get("steps") or []:
            value = step.get("error")
            if value and str(value) not in errors:
                errors.append(str(value))
        return errors

    def _goal_status_progress(
        self,
        goal: dict[str, Any],
        plan: dict[str, Any] | None,
        project: dict[str, Any] | None,
    ) -> int:
        if project and project.get("progress") is not None:
            return int(project["progress"])
        plan_status = str((plan or {}).get("status") or "")
        if plan_status in PLAN_TERMINAL_STATUSES or goal.get("status") == "completed":
            return 100
        return int(float(goal.get("progress") or 0) * 100)

    async def _run_agent_build(
        self,
        plan: dict[str, Any],
        *,
        approved_by: str,
        source_channel: str,
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        kwargs = {
            "requested_by": approved_by,
            "source_channel": source_channel or "api",
            "build_mode": self._agent_build_mode_for_source(source_channel),
            "tracking_context": {
                "goal_id": plan.get("goal_id"),
                "plan_id": plan.get("id"),
                "agent_request_id": proposal.get("agent_request_id"),
                "internal_capability_request": plan.get(
                    "internal_capability_request",
                    False,
                ),
                "capability_request_id": plan.get("capability_request_id"),
                "creation_type": plan.get("creation_type"),
                "capability_original_text": plan.get("capability_original_text"),
                "required_tools": list(plan.get("required_tools") or []),
                "created_tools": list(plan.get("created_tools") or []),
                "tool_creation_status": plan.get(
                    "tool_creation_status",
                    "not_required",
                ),
            },
        }
        try:
            return await self.agent_build_orchestrator.build_from_request(
                str(plan.get("goal") or ""),
                **kwargs,
            )
        except TypeError as exc:
            if "tracking_context" not in str(exc):
                raise
            kwargs.pop("tracking_context")
            return await self.agent_build_orchestrator.build_from_request(
                str(plan.get("goal") or ""),
                **kwargs,
            )

    def _agent_build_mode_for_source(self, source_channel: str) -> str:
        if source_channel in {"telegram", "validation", "telegram_validation"}:
            return "hybrid"
        return "deterministic"

    def _extract_agent_proposal(self, result: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return None

        direct = result.get("agent_creation_proposal") or result.get("proposal")
        if isinstance(direct, dict):
            return direct

        nested = result.get("result")
        if isinstance(nested, dict):
            proposal = nested.get("agent_creation_proposal") or nested.get("proposal")
            if isinstance(proposal, dict):
                return proposal

        return None

    def _project_relative_path(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(Path("/etc/neron").resolve()))
        except ValueError:
            return path

    def _requires_agent_draft(self, plan: dict[str, Any]) -> bool:
        return any(
            step.get("agent") == "agent_creator" or step.get("action") == "create_skeleton"
            for step in plan.get("steps", [])
        )

    def _is_agent_creation_plan(self, plan: dict[str, Any]) -> bool:
        return any(
            step.get("agent") == "agent_creator"
            or step.get("action") in {"define_agent", "create_skeleton"}
            for step in plan.get("steps", [])
        )

    def _goal_response(
        self,
        status: str,
        goal: dict[str, Any],
        plan: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        proposal = plan.get("agent_creation_proposal")
        return {
            "status": status,
            "goal": goal,
            "goal_id": goal.get("id"),
            "planner_called": bool(plan.get("planner_called") or plan.get("id")),
            "plan_id": plan.get("id"),
            "plan": plan,
            "agent_creator_called": self._agent_creator_called(plan, tasks),
            "agent_request_id": plan.get("agent_request_id")
            or (proposal or {}).get("agent_request_id"),
            "proposal": proposal,
            "tasks": tasks,
        }

    def _agent_creator_called(
        self,
        plan: dict[str, Any],
        tasks: list[dict[str, Any]],
    ) -> bool:
        if plan.get("agent_creator_called") is True or plan.get("agent_creation_proposal"):
            return True
        if any(step.get("agent") == "agent_creator" for step in plan.get("steps", [])):
            return any(task.get("status") == "completed" for task in tasks)
        return False

    async def _publish_flow_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            await event_bus.publish(Event(type=event_type, source="goal_orchestrator", payload=payload))
        except Exception:
            return

    def _tasks_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        return [
            task
            for task in self.task_manager.list_tasks()
            if str(task.get("plan_id") or "") == plan_id
        ]

    def _decide(self, risk: dict[str, Any], sensitive_detected: bool) -> str:
        score = int(risk.get("risk_score") or 0)

        if sensitive_detected or score > 95:
            return "blocked"
        if score > 80:
            return "approval_required"
        if risk.get("execution_allowed") is True and score <= 50:
            return "auto_execute"
        return "approval_required"

    def _detect_sensitive_action(self, plan: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        searchable = [str(plan.get("goal") or "")]

        for step in plan.get("steps", []):
            action = str(step.get("action") or "").lower()
            if action in SENSITIVE_ACTIONS:
                reasons.append(f"Action sensible détectée : {action}.")
            searchable.extend(
                [
                    str(step.get("title") or ""),
                    str(step.get("description") or ""),
                    action,
                    str(step.get("agent") or ""),
                ]
            )

        text = "\n".join(searchable).lower()
        for keyword in SENSITIVE_KEYWORDS:
            if keyword in text:
                reasons.append(f"Mot-clé sensible détecté : {keyword}.")

        return {"detected": bool(reasons), "reasons": sorted(set(reasons))}

    def _complete_goal(self, plan: dict[str, Any]) -> None:
        goal_id = plan.get("goal_id")
        if goal_id:
            self.goal_manager.update_progress(str(goal_id), 1.0)
            self._update_goal_status(
                str(goal_id),
                "completed",
                current_step="completed",
            )
            execution_engine = self._execution_engine_or_none()
            if execution_engine is not None:
                execution_engine.complete_goal(str(goal_id))

    def _execution_engine(self) -> GoalExecutionEngine:
        engine = self._execution_engine_or_none()
        if engine is None:
            raise RuntimeError("Goal execution engine requires a SQLite-backed manager")
        return engine

    def _execution_engine_or_none(self) -> GoalExecutionEngine | None:
        engine = getattr(self, "execution_engine", None)
        if engine is not None:
            return engine
        sqlite_store = getattr(self.goal_manager, "sqlite_store", None)
        if sqlite_store is None:
            return None
        engine = GoalExecutionEngine(sqlite_store)
        self.execution_engine = engine
        return engine

    def _update_goal_status(
        self,
        goal_id: str,
        status: str,
        *,
        current_step: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        try:
            return self.goal_manager.update_status(
                goal_id,
                status,
                current_step=current_step,
                error=error,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            return self.goal_manager.update_status(goal_id, status)

    async def _notify(self, message: str, level: str = "info") -> None:
        if self.notifier:
            await self.notifier(message, level)
            return

        try:
            from agents.builtin.communication.telegram_agent import send_notification

            await send_notification(message, level=level)
        except Exception:
            return

    async def _notify_approval_required(self, plan: dict[str, Any]) -> None:
        risk = plan.get("risk") or {}
        short_id = str(plan.get("id") or "")[:8]
        await self._notify(
            "⚠ Validation requise\n\n"
            f"Objectif :\n{plan.get('goal')}\n\n"
            f"Risque :\n{risk.get('risk_level', 'unknown')} ({risk.get('risk_score', '?')}/100)\n\n"
            f"Approuver :\n/approve {short_id}\n\n"
            f"Refuser :\n/refuse {short_id}",
            level="warning",
        )

    async def _notify_blocked(self, plan: dict[str, Any]) -> None:
        risk = plan.get("risk") or {}
        reasons = risk.get("sensitive_reasons") or risk.get("risks") or ["Risque critique détecté."]
        await self._notify(
            "🚫 Exécution interdite\n\n"
            f"Objectif :\n{plan.get('goal')}\n\n"
            f"Risque :\n{risk.get('risk_level', 'critical')} ({risk.get('risk_score', '?')}/100)\n\n"
            f"Raison :\n{'; '.join(str(reason) for reason in reasons[:3])}",
            level="error",
        )

    async def _notify_execution_started(self, plan: dict[str, Any]) -> None:
        await self._notify(
            "⚙ Exécution démarrée\n"
            f"Objectif : {plan.get('goal')}",
            level="info",
        )

    async def _notify_task_done(
        self,
        plan: dict[str, Any],
        task: dict[str, Any],
        index: int,
        total: int,
        skipped: bool = False,
    ) -> None:
        icon = "⏭" if skipped else "✅"
        label = "Tâche ignorée" if skipped else "Tâche terminée"
        await self._notify(
            f"{icon} {label} {index}/{total}\n{task.get('title')}",
            level="info",
        )

    async def _notify_task_failed(
        self,
        plan: dict[str, Any],
        task: dict[str, Any],
        index: int,
        total: int,
        error: str,
    ) -> None:
        await self._notify(
            f"❌ Tâche échouée {index}/{total}\n{task.get('title')}\nErreur : {error}",
            level="error",
        )

    async def _notify_execution_finished(self, plan: dict[str, Any]) -> None:
        await self._notify(
            self._format_execution_report(plan, "🏁 Objectif terminé"),
            level="info",
        )

    async def _notify_execution_partial(self, plan: dict[str, Any]) -> None:
        await self._notify(
            self._format_execution_report(plan, "⚠ Objectif partiel"),
            level="warning",
        )

    async def _notify_execution_failed(self, plan: dict[str, Any]) -> None:
        await self._notify(
            self._format_execution_report(plan, "❌ Exécution échouée"),
            level="error",
        )

    def _format_execution_report(self, plan: dict[str, Any], title: str) -> str:
        risk = plan.get("risk") or {}
        summary = plan.get("execution_summary") or {}
        agent_path = plan.get("agent_path")
        proposal = plan.get("agent_creation_proposal") or {}
        result_lines = [
            "✓ Plan généré",
            "✓ Risque validé",
        ]

        if agent_path:
            if plan.get("agent_state") == "registered":
                result_lines.append("✓ Agent enregistré")
            else:
                result_lines.append("✓ Brouillon créé")
        if proposal:
            result_lines.append("✓ Proposition d'agent créée")
        if plan.get("tests_ok") is True:
            result_lines.append("✓ Tests OK")
        if (plan.get("runtime_reload") or {}).get("ok") is True:
            result_lines.append("✓ Runtime rechargé")

        if plan.get("status") == "partial":
            result_lines.append("⚠ Certaines tâches ignorées")

        if plan.get("status") == "failed":
            result_lines.append("✗ Exécution incomplète")

        lines = [
            title,
            "",
            "Objectif :",
            str(plan.get("goal")),
            "",
            f"Plan ID : {plan.get('id')}",
            f"Risque : {risk.get('risk_level', 'unknown')} ({risk.get('risk_score', '?')}/100)",
            "",
            "Résultat :",
            *result_lines,
            "",
            "Tâches :",
            f"completed : {summary.get('completed', 0)}",
            f"skipped : {summary.get('skipped', 0)}",
            f"failed : {summary.get('failed', 0)}",
        ]

        if agent_path:
            lines.extend(
                [
                    "",
                    "Agent :",
                    str(agent_path),
                    "",
                    "État :",
                    str(plan.get("agent_state") or "draft_only"),
                ]
            )

        if plan.get("registered_agent"):
            lines.extend(
                [
                    "",
                    "Agent enregistré :",
                    str(plan.get("registered_agent")),
                ]
            )

        if proposal:
            lines.extend(
                [
                    "",
                    "Proposition :",
                    str(proposal.get("agent_name")),
                    "",
                    "État :",
                    str(proposal.get("status")),
                ]
            )

        if plan.get("error"):
            lines.extend(["", "Erreur :", str(plan.get("error"))])

        return "\n".join(lines)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


def run_async(coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    raise RuntimeError("run_async ne peut pas être utilisé dans une boucle asyncio active.")


def get_goal_orchestrator(notifier: Notifier | None = None) -> GoalOrchestrator:
    return GoalOrchestrator(notifier=notifier)
