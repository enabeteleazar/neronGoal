"""Planning package — exports paresseux (PEP 562).

``goal.planning.storage`` importe encore le monolithe (modules.events,
core.storage) : il ne doit être chargé qu'à la demande, jamais au
simple import du package.
"""
from __future__ import annotations

__all__ = ["AutonomousPlanner", "Plan", "PlanStep", "StepStatus", "PlanStorage"]


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
