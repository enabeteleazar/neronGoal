from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    title: str
    description: str
    agent: str | None = None
    action: str | None = None
    status: StepStatus = StepStatus.PENDING
    result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep]
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "pending"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "created_at": self.created_at,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
        }
