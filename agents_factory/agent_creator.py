from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from server.common.paths import NERON_DATA_DIR, NERON_ROOT

DEFAULT_PROPOSALS_PATH = Path(
    os.getenv(
        "NERON_AGENT_PROPOSALS_PATH",
        str(NERON_DATA_DIR / "agent_creator_proposals.jsonl"),
    )
)


class AgentCreator:
    """
    Facade sure de creation d'agent.

    Elle ne genere pas, ne modifie pas et n'execute pas de code. Elle prepare
    uniquement une proposition traçable en attente de validation humaine.
    """

    def __init__(
        self,
        proposals_path: Path = DEFAULT_PROPOSALS_PATH,
        project_root: Path | None = None,
    ) -> None:
        self.proposals_path = proposals_path
        self.project_root = project_root or NERON_ROOT

    def request_agent_creation(
        self,
        *,
        goal: str,
        plan: dict[str, Any],
        missing_capability: str | None = None,
        required_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        existing = self._find_existing_proposal(plan)
        if existing:
            return existing

        plan_id = str(plan.get("id") or "")
        goal_id = str(plan.get("goal_id") or "")
        agent_name = self._agent_name_from_goal(goal, missing_capability)
        purpose = self._purpose_from_goal(goal, agent_name)

        proposal = {
            "agent_request_id": str(uuid4()),
            "agent_name": agent_name,
            "goal": goal,
            "purpose": purpose,
            "required_capabilities": self._required_capabilities(agent_name, missing_capability),
            "required_tools": list(required_tools or plan.get("required_tools") or []),
            "proposed_files": [
                f"workspace/agents/{agent_name}.py",
                f"tests/test_{agent_name}.py",
            ],
            "endpoints_or_hooks": [
                "agents.runtime.runtime",
                "agents.factory.promotion.AgentPromotionService",
            ],
            "tests_to_create": [
                f"tests/test_{agent_name}.py",
            ],
            "risk_level": "low",
            "status": "pending_human_validation",
            "human_validation_required": True,
            "code_execution_allowed": False,
            "applied_to_core": False,
            "created_from_goal_id": goal_id,
            "created_from_plan_id": plan_id,
            "missing_capability": missing_capability or self.infer_missing_capability(goal),
            "codex_ready": True,
            "codex_auto_run": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        self._append(proposal)
        return proposal

    def scan_existing_agents(self) -> dict[str, Any]:
        candidates: list[str] = []
        for relative in (
            "core/agents",
            "data/generated_agents",
            "workspace/agents",
            "workspace/agent_drafts",
        ):
            root = self.project_root / relative
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                if path.name.startswith("_"):
                    continue
                try:
                    candidates.append(str(path.relative_to(self.project_root)))
                except ValueError:
                    candidates.append(str(path))

        return {
            "status": "success",
            "agents_scanned": len(candidates),
            "files": candidates[:200],
            "code_execution_allowed": False,
        }

    def infer_missing_capability(self, goal: str) -> str:
        normalized = self._normalize(goal)
        if any(word in normalized for word in ("meteo", "weather")):
            return "weather_request_handling"
        if "wwdc" in normalized or "apple" in normalized:
            return "apple_event_watch"
        if "test" in normalized:
            return "test_agent_execution"
        return "custom_agent_capability"

    def _find_existing_proposal(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        plan_id = str(plan.get("id") or "")
        if not plan_id or not self.proposals_path.exists():
            return None

        for line in reversed(self.proposals_path.read_text(encoding="utf-8").splitlines()):
            try:
                proposal = json.loads(line)
            except json.JSONDecodeError:
                continue
            if proposal.get("created_from_plan_id") == plan_id:
                return proposal
        return None

    def list_proposals(self) -> list[dict[str, Any]]:
        if not self.proposals_path.exists():
            return []

        proposals: list[dict[str, Any]] = []
        for line in self.proposals_path.read_text(encoding="utf-8").splitlines():
            try:
                proposal = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(proposal, dict):
                proposals.append(proposal)
        return proposals

    def get_proposal(self, agent_request_id: str) -> dict[str, Any] | None:
        for proposal in reversed(self.list_proposals()):
            if str(proposal.get("agent_request_id") or "") == agent_request_id:
                return proposal
        return None

    def update_proposal(
        self,
        agent_request_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        proposals = self.list_proposals()
        updated: dict[str, Any] | None = None

        for index, proposal in enumerate(proposals):
            if str(proposal.get("agent_request_id") or "") != agent_request_id:
                continue

            merged = {
                **proposal,
                **updates,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            proposals[index] = merged
            updated = merged

        if updated is None:
            return None

        self.proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with self.proposals_path.open("w", encoding="utf-8") as handle:
            for proposal in proposals:
                handle.write(json.dumps(proposal, ensure_ascii=False) + "\n")

        return updated

    def _append(self, proposal: dict[str, Any]) -> None:
        self.proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with self.proposals_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(proposal, ensure_ascii=False) + "\n")

    def _agent_name_from_goal(self, goal: str, missing_capability: str | None) -> str:
        explicit_name = self._explicit_agent_name(goal)
        if explicit_name:
            return explicit_name

        normalized = self._normalize(" ".join(part for part in (goal, missing_capability or "") if part))
        if any(word in normalized for word in ("meteo", "weather")):
            if self._is_agriculture_weather_goal(normalized):
                return "agriculture_weather_agent"
            return "weather_agent"
        if "wwdc" in normalized or "apple" in normalized:
            return "wwdc_agent"
        if "test" in normalized:
            return "test_agent"

        ignored = {
            "creer",
            "create",
            "agent",
            "un",
            "une",
            "de",
            "du",
            "des",
            "pour",
            "qui",
            "capable",
            "repondre",
            "demande",
            "simple",
        }
        words = [word for word in re.findall(r"[a-z0-9]+", normalized) if word not in ignored]
        base = "_".join(words[:3]) or "custom"
        return base if base.endswith("_agent") else f"{base}_agent"

    def _explicit_agent_name(self, goal: str) -> str | None:
        normalized = self._normalize(goal)
        patterns = (
            r"\bnomme\s+(\S+)",
            r"\bappele\s+(\S+)",
            r"\bappelle\s+(\S+)",
            r"\bs\s+appelle\s+(\S+)",
            r"\bnamed\s+(\S+)",
            r"\bname\s+(\S+)",
        )

        for pattern in patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            candidate = self._clean_agent_name(match.group(1))
            if candidate:
                return candidate

        return None

    def _clean_agent_name(self, value: str) -> str | None:
        candidate = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
        if not candidate:
            return None
        if candidate[0].isdigit():
            candidate = f"agent_{candidate}"
        return candidate

    def _purpose_from_goal(self, goal: str, agent_name: str) -> str:
        if agent_name == "agriculture_weather_agent":
            return "Surveiller et repondre aux demandes meteo agricoles"
        if agent_name == "weather_agent":
            return "Repondre aux demandes meteo simples"
        return f"Traiter l'objectif : {goal}"

    def _required_capabilities(
        self,
        agent_name: str,
        missing_capability: str | None,
    ) -> list[str]:
        if agent_name == "agriculture_weather_agent":
            return [
                "parse_agriculture_weather_request",
                "static_agriculture_weather_fallback",
                "format_agriculture_weather_response",
            ]

        if agent_name == "weather_agent":
            return [
                "parse_weather_request",
                "fetch_weather_data",
                "format_weather_response",
            ]

        capability = missing_capability or "handle_custom_request"
        return [
            "parse_user_request",
            capability,
            "format_agent_response",
        ]

    def _normalize(self, value: str) -> str:
        normalized = unicodedata.normalize("NFD", value.lower())
        return "".join(
            char
            for char in normalized
            if unicodedata.category(char) != "Mn"
        )

    def _is_agriculture_weather_goal(self, normalized: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", normalized))
        return bool(
            words
            & {
                "agriculture",
                "agricole",
                "agricoles",
                "agri",
                "culture",
                "cultures",
                "champ",
                "champs",
                "irrigation",
                "recolte",
                "recoltes",
                "crop",
                "crops",
                "farm",
                "farming",
            }
        )
