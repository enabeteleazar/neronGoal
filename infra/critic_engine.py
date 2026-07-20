"""Moteur critique du service goal (rapatrié depuis modules/cognitive).

Les helpers d'historique (append_jsonl, compaction) sont réimplémentés
localement pour couper la dépendance au monolithe. L'historique est
écrit dans le répertoire de données DU GOAL.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.common.paths import NERON_DATA_DIR


CRITIC_HISTORY_PATH = Path(
    os.getenv(
        "NERON_GOAL_CRITIC_HISTORY_PATH",
        str(NERON_DATA_DIR / "goal" / "critic_history.jsonl"),
    )
)

_MAX_FIELD_LEN = 500


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _compact(state: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, str) and len(value) > _MAX_FIELD_LEN:
            compacted[key] = value[:_MAX_FIELD_LEN] + "…"
        else:
            compacted[key] = value
    return compacted


class CriticEngine:
    """Moteur critique minimal de Néron (périmètre goal)."""

    def evaluate_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Évalue le risque d'un plan Planner avant exécution."""
        risk_score = 0
        risks: list[str] = []
        recommendations: list[str] = []

        steps = plan.get("steps", [])
        if not steps:
            risk_score += 40
            risks.append("Le plan ne contient aucune étape.")
            recommendations.append("Refuser l'exécution tant que le plan est vide.")

        sensitive_detected = False
        sensitive_actions = {
            "apply_patch",
            "write_file",
            "delete_file",
            "modify_core",
            "modify_system_config",
            "modify_systemd",
            "read_secret",
            "write_secret",
            "modify_secret",
            "modify_security",
            "apply_destructive_change",
        }
        sensitive_keywords = {
            "suppression",
            "supprimer",
            "delete",
            "destructive",
            "destructif",
            "systemd",
            "secret",
            "token",
            "ssh",
            "sécurité",
            "security",
        }

        for step in steps:
            action = step.get("action")
            agent = step.get("agent")
            text = " ".join(
                str(step.get(field) or "")
                for field in ("title", "description", "action", "agent")
            ).lower()

            if action in sensitive_actions:
                sensitive_detected = True
                risk_score += 50
                risks.append(f"Action sensible détectée : {action}.")
                recommendations.append("Bloquer l'exécution automatique.")

            for keyword in sensitive_keywords:
                if keyword in text:
                    sensitive_detected = True
                    risks.append(f"Mot-clé sensible détecté : {keyword}.")

            if agent in {"code_agent", "agent_creator"}:
                risk_score += 15
                risks.append(f"Agent pouvant produire du code : {agent}.")
                recommendations.append("Limiter l'écriture au dossier workspace.")

            if action in {"run_tests", "prepare_tests"}:
                risk_score += 5

        if sensitive_detected:
            risk_score = max(risk_score, 90)

        if plan.get("approval_required") is True and plan.get("approved") is not True:
            risk_score += 20
            risks.append("Le plan n'est pas approuvé.")
            recommendations.append("Demander une approbation avant exécution.")

        risks = list(dict.fromkeys(risks))
        recommendations = list(dict.fromkeys(recommendations))

        if sensitive_detected or risk_score > 80:
            level = "critical"
            execution_allowed = False
        elif risk_score > 30:
            level = "medium"
            execution_allowed = True
        else:
            level = "low"
            execution_allowed = True

        result = {
            "risk_score": min(risk_score, 100),
            "risk_level": level,
            "execution_allowed": execution_allowed,
            "sensitive_action_detected": sensitive_detected,
            "risks": risks,
            "recommendations": recommendations,
        }
        self.save_evaluation(
            {"type": "plan_risk", "plan_id": plan.get("id"), "goal": plan.get("goal")},
            result,
        )
        return result

    def save_evaluation(
        self,
        cognitive_state: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        _append_jsonl(
            CRITIC_HISTORY_PATH,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cognitive_state": _compact(cognitive_state),
                "result": result,
            },
        )


_critic_engine: CriticEngine | None = None


def get_critic_engine() -> CriticEngine:
    global _critic_engine
    if _critic_engine is None:
        _critic_engine = CriticEngine()
    return _critic_engine
