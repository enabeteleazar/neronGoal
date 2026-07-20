"""Goal Engine package for NéronOS.

Les exports sont paresseux (PEP 562) : importer ``goal`` ou ``goal.app``
ne doit JAMAIS tirer le monolithe (modules.*, core.*, agents.*).
Ces dépendances ne sont chargées que si l'on accède explicitement
aux symboles concernés (ex. PlanStorage).
"""
from __future__ import annotations

__all__ = [
    "AutonomousPlanner",
    "Plan",
    "PlanStep",
    "StepStatus",
    "PlanStorage",
]


def __getattr__(name: str):
    if name in {"Plan", "PlanStep", "StepStatus"}:
        from goal.planning import models

        return getattr(models, name)
    if name == "AutonomousPlanner":
        from goal.planning.planner import AutonomousPlanner

        return AutonomousPlanner
    if name == "PlanStorage":
        from goal.planning.storage import PlanStorage

        return PlanStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
