from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.factory.agent_creator import AgentCreator


class PlanExecutor:
    def __init__(
        self,
        project_root: Path | None = None,
        agent_creator: AgentCreator | None = None,
    ):
        if project_root is None:
            project_root = Path(os.getenv("NERON_PROJECT_ROOT", Path.cwd()))
        self.project_root = project_root
        self.draft_dir = self.project_root / "workspace" / "agent_drafts"
        self.draft_dir.mkdir(parents=True, exist_ok=True)
        self.agent_creator = agent_creator or AgentCreator(project_root=self.project_root)

    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not plan.get("approved"):
            plan["status"] = "approval_required"
            plan["error"] = "Plan non approuvé."
            return plan

        plan["status"] = "running"
        plan["executed_at"] = datetime.now(timezone.utc).isoformat()

        completed = 0
        skipped = 0

        for step in plan.get("steps", []):
            step["status"] = "running"

            try:
                step["result"] = self._execute_step(step, plan)
                if step["result"].get("status") == "skipped":
                    step["status"] = "skipped"
                    skipped += 1
                else:
                    step["status"] = "completed"
                    completed += 1
                step["error"] = None
            except Exception as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
                plan["status"] = "failed"
                return plan

        plan["task_counts"] = {
            "total": completed + skipped,
            "completed": completed,
            "skipped": skipped,
            "failed": 0,
        }

        if skipped and not completed:
            plan["status"] = "partial"
            plan["error"] = "Aucune étape exécutable n'a été terminée."
            return plan

        plan["status"] = "plan_finished"
        plan["error"] = None
        return plan

    def _execute_step(self, step: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        action = step.get("action")

        if action in {"analyze_agents", "check_integration", "scan_project"}:
            return self._scan_agents()

        if action in {"define_agent", "create_skeleton"}:
            return self._create_agent_draft(plan)

        if action in {"prepare_tests", "run_tests"}:
            return self._run_tests()

        if action in {"evaluate_risk", "evaluate_plan", "security_audit"}:
            return {
                "risk": "medium",
                "requires_human_review": True,
                "reason": "Modification du Core interdite en mode Executor V1.",
            }

        return {
            "status": "skipped",
            "reason": "Action non implémentée dans Executor V1.",
        }

    def _scan_agents(self) -> dict[str, Any]:
        agents_dir = self.project_root / "agents"
        files = []

        if agents_dir.exists():
            files = sorted(
                str(path.relative_to(self.project_root))
                for path in agents_dir.rglob("*.py")
            )

        return {
            "agents_dir": str(agents_dir),
            "files_found": len(files),
            "sample": files[:20],
        }

    def _create_agent_draft(self, plan: dict[str, Any]) -> dict[str, Any]:
        goal = str(plan.get("goal") or "nouvel agent")
        proposal = self.agent_creator.request_agent_creation(
            goal=goal,
            plan=plan,
            missing_capability=self.agent_creator.infer_missing_capability(goal),
        )
        agent_name = str(proposal.get("agent_name") or self._safe_agent_name(goal))
        safe_name = self._safe_agent_name(agent_name)
        class_name = self._class_name_from_safe_name(safe_name)
        draft_path = self.draft_dir / f"{safe_name}.py"
        draft_path.write_text(
            self._render_agent_draft(
                class_name=class_name,
                agent_name=safe_name,
                goal=goal,
                proposal=proposal,
            ),
            encoding="utf-8",
        )
        relative_path = str(draft_path.relative_to(self.project_root))

        return {
            "status": "success",
            "proposal_created": True,
            "agent_creation_proposal": proposal,
            "proposal": proposal,
            "agent_request_id": proposal.get("agent_request_id"),
            "agent_path": relative_path,
            "draft_created": True,
            "draft_only": True,
            "state": "draft_only",
            "applied_to_core": False,
            "code_executed": False,
        }

    def _safe_agent_name(self, goal: str) -> str:
        normalized = goal.lower()
        normalized = normalized.replace("é", "e").replace("è", "e").replace("ê", "e")
        normalized = normalized.replace("à", "a").replace("ç", "c")
        words = re.findall(r"[a-z0-9]+", normalized)
        ignored = {
            "creer",
            "create",
            "un",
            "une",
            "agent",
            "de",
            "du",
            "des",
            "pour",
            "qui",
            "me",
            "la",
            "le",
            "les",
            "automatique",
            "nouvel",
            "nouveau",
        }
        useful = [word for word in words if word not in ignored][:3]
        base = "_".join(useful) or "generated"

        if not base.endswith("_agent"):
            base = f"{base}_agent"

        return base

    def _class_name_from_safe_name(self, safe_name: str) -> str:
        return "".join(part.capitalize() for part in safe_name.split("_") if part)

    def _render_agent_draft(
        self,
        *,
        class_name: str,
        agent_name: str,
        goal: str,
        proposal: dict[str, Any],
    ) -> str:
        capabilities = proposal.get("required_capabilities") or []
        return (
            "from __future__ import annotations\n\n\n"
            f"class {class_name}:\n"
            f"    name = {agent_name!r}\n"
            "    state = 'draft_only'\n\n"
            "    def __init__(self):\n"
            f"        self.goal = {goal!r}\n"
            f"        self.required_capabilities = {capabilities!r}\n\n"
            "    async def run(self, action: str | None = None, params: dict | None = None) -> dict:\n"
            "        return {\n"
            "            'agent': self.name,\n"
            "            'action': action,\n"
            "            'params': params or {},\n"
            "            'status': self.state,\n"
            "            'goal': self.goal,\n"
            "            'required_capabilities': self.required_capabilities,\n"
            "        }\n"
        )

    def _run_tests(self) -> dict[str, Any]:
        result = subprocess.run(
            ["python3", "-m", "pytest", "-q"],
            cwd=self.project_root,
            text=True,
            capture_output=True,
            timeout=120,
        )

        return {
            "command": "python3 -m pytest -q",
            "returncode": result.returncode,
            "success": result.returncode == 0,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
