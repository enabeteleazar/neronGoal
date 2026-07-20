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


# ═══════════════════════════════════════════════════════════════════
# Mots-clés sensibles — SOURCE DE VÉRITÉ UNIQUE pour tout le service goal.
#
# Avant cette fusion, deux listes indépendantes existaient : celle-ci
# (11 termes) et SENSITIVE_BUILD_KEYWORDS dans build_orchestrator.py
# (23 termes). Ni l'une ni l'autre n'était un sur-ensemble de l'autre —
# un objectif contenant "sudo" bloquait dans l'une, "ssh" bloquait dans
# l'autre, mais pas les deux. Fusionné ici en union complète : plus
# aucun terme précédemment détecté par l'une des deux listes ne peut
# être manqué. build_orchestrator.py doit importer SENSITIVE_KEYWORDS
# d'ici plutôt que de garder sa propre liste (phase 8).
# ═══════════════════════════════════════════════════════════════════

import unicodedata


def normalize_for_keyword_match(text: str) -> str:
    """Normalisation partagée : minuscules + accents supprimés (NFKD).

    IMPORTANT : avant cette fonction, CriticEngine ne faisait que
    `.lower()` — un objectif écrit "Sécurité" (avec accent) ne
    matchait pas de façon fiable selon la forme exacte de la chaîne.
    build_orchestrator.py, lui, normalisait déjà ainsi. Unifié ici :
    les deux composants matchent désormais exactement les mêmes
    variantes accentuées ou non.
    """
    stripped = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in stripped if unicodedata.category(char) != "Mn")


# Union de TROIS listes qui avaient divergé indépendamment :
#   - CriticEngine (ici)                    : 11 termes, sensible aux accents
#   - SENSITIVE_KEYWORDS (goal_orchestrator.py, déployé) : 24 termes, sensible aux accents
#   - SENSITIVE_BUILD_KEYWORDS (build_orchestrator.py, pas encore migré) : 23 termes, déjà normalisé
# Aucune des trois n'était un sur-ensemble des deux autres.
SENSITIVE_KEYWORDS: frozenset[str] = frozenset({
    "suppression", "supprimer", "delete", "remove",
    "destructive", "destructif",
    "systemd", "service systeme", "services systeme", "service systemd",
    "configuration systeme", "core critique",
    "secret", "secrets", "token", "tokens",
    "ssh", "cle ssh", "ssh key", "api key",
    "securite", "security",
    "rm -rf", "rm rf", "chmod", "sudo",
    "fichier sensible", "fichiers sensibles",
    "shell", "executer du code", "execute code",
})

# Union des deux listes d'ACTIONS (noms d'actions de step de plan, distinct
# des mots-clés en texte libre ci-dessus) : CriticEngine (11) +
# goal_orchestrator.py (14). Même principe : union complète, jamais de perte
# de couverture.
SENSITIVE_ACTIONS: frozenset[str] = frozenset({
    "apply_patch", "write_file", "delete_file", "remove_file",
    "rm", "unlink",
    "modify_core", "modify_system_config", "modify_systemd",
    "restart_systemd",
    "read_secret", "write_secret", "modify_secret", "modify_security",
    "apply_destructive_change", "destructive_action",
})



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
        for step in steps:
            action = step.get("action")
            agent = step.get("agent")
            raw_text = " ".join(
                str(step.get(field) or "")
                for field in ("title", "description", "action", "agent")
            )
            # Normalisation partagée avec build_orchestrator.py (phase 8) :
            # accents supprimés, en plus du passage en minuscules — un
            # mot-clé accentué écrit différemment ne peut plus échapper
            # à la détection (cf. normalize_for_keyword_match ci-dessus).
            text = normalize_for_keyword_match(raw_text)

            if action in SENSITIVE_ACTIONS:
                sensitive_detected = True
                risk_score += 50
                risks.append(f"Action sensible détectée : {action}.")
                recommendations.append("Bloquer l'exécution automatique.")

            for keyword in SENSITIVE_KEYWORDS:
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
