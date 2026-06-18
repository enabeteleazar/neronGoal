from __future__ import annotations

from goal.planning.models import Plan, PlanStep


class AutonomousPlanner:
    """
    Planner autonome V1.

    Objectif :
    - transformer un objectif en plan structuré ;
    - ne rien exécuter automatiquement ;
    - préparer la connexion au GoalManager, TaskManager et Critic.
    """

    def create_plan(self, goal: str) -> Plan:
        normalized = goal.lower().strip()

        if not normalized:
            return self._empty_plan()

        if "agent" in normalized:
            return self._agent_plan(goal)

        if "audit" in normalized or "sécurité" in normalized or "securite" in normalized:
            return self._audit_plan(goal)

        if "bug" in normalized or "corrige" in normalized or "erreur" in normalized:
            return self._fix_plan(goal)

        return self._generic_plan(goal)

    def _empty_plan(self) -> Plan:
        return Plan(
            goal="",
            status="invalid",
            steps=[
                PlanStep(
                    title="Objectif manquant",
                    description="Aucun objectif exploitable n'a été fourni.",
                    agent=None,
                    action=None,
                )
            ],
        )

    def _agent_plan(self, goal: str) -> Plan:
        return Plan(
            goal=goal,
            steps=[
                PlanStep(
                    title="Analyser les agents existants",
                    description="Identifier la structure actuelle des agents de Néron.",
                    agent="code_audit_agent",
                    action="analyze_agents",
                ),
                PlanStep(
                    title="Définir le rôle du nouvel agent",
                    description="Déterminer sa mission, ses entrées, ses sorties et ses limites.",
                    agent="agent_creator",
                    action="define_agent",
                ),
                PlanStep(
                    title="Préparer le squelette de l'agent",
                    description="Préparer les fichiers nécessaires sans les exécuter automatiquement.",
                    agent="agent_creator",
                    action="create_skeleton",
                ),
                PlanStep(
                    title="Vérifier l'intégration",
                    description="Contrôler que l'agent respecte l'architecture existante.",
                    agent="code_audit_agent",
                    action="check_integration",
                ),
            ],
        )

    def _audit_plan(self, goal: str) -> Plan:
        return Plan(
            goal=goal,
            steps=[
                PlanStep(
                    title="Scanner le projet",
                    description="Analyser les fichiers principaux de Néron.",
                    agent="code_audit_agent",
                    action="scan_project",
                ),
                PlanStep(
                    title="Chercher les doublons",
                    description="Identifier les fichiers, routes ou modules redondants.",
                    agent="code_audit_agent",
                    action="find_duplicates",
                ),
                PlanStep(
                    title="Chercher les risques de sécurité",
                    description="Repérer les secrets, permissions dangereuses et endpoints sensibles.",
                    agent="code_audit_agent",
                    action="security_audit",
                ),
                PlanStep(
                    title="Produire un rapport",
                    description="Créer un résumé exploitable pour correction.",
                    agent="memory_agent",
                    action="write_report",
                ),
            ],
        )

    def _fix_plan(self, goal: str) -> Plan:
        return Plan(
            goal=goal,
            steps=[
                PlanStep(
                    title="Comprendre le problème",
                    description="Identifier le symptôme, les logs et le module concerné.",
                    agent="code_audit_agent",
                    action="diagnose_issue",
                ),
                PlanStep(
                    title="Proposer une correction",
                    description="Préparer une solution minimale et réversible.",
                    agent="code_agent",
                    action="propose_patch",
                ),
                PlanStep(
                    title="Valider le risque",
                    description="Évaluer si la correction peut casser une autre partie de Néron.",
                    agent="critic_agent",
                    action="evaluate_risk",
                ),
                PlanStep(
                    title="Préparer les tests",
                    description="Définir les commandes de test à lancer après correction.",
                    agent="system_agent",
                    action="prepare_tests",
                ),
            ],
        )

    def _generic_plan(self, goal: str) -> Plan:
        return Plan(
            goal=goal,
            steps=[
                PlanStep(
                    title="Comprendre l'objectif",
                    description="Clarifier ce que Néron doit atteindre.",
                    agent="llm_agent",
                    action="analyze_goal",
                ),
                PlanStep(
                    title="Décomposer l'objectif",
                    description="Transformer l'objectif en étapes simples.",
                    agent="llm_agent",
                    action="decompose_goal",
                ),
                PlanStep(
                    title="Identifier les risques",
                    description="Lister les impacts possibles avant exécution.",
                    agent="critic_agent",
                    action="evaluate_plan",
                ),
            ],
        )
