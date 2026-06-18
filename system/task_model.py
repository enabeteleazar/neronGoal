from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


VALID_TASK_STATUSES = {
    "pending",
    "running",
    "done",
    "failed",
    "cancelled",
}


VALID_TASK_PRIORITIES = {
    "low",
    "normal",
    "high",
    "critical",
}


@dataclass
class Task:
    id: str
    title: str
    description: str
    status: str
    priority: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]

    @staticmethod
    def create(
        title: str,
        description: str = "",
        priority: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> "Task":
        now = datetime.now(timezone.utc).isoformat()

        if priority not in VALID_TASK_PRIORITIES:
            priority = "normal"

        return Task(
            id=str(uuid4()),
            title=title.strip(),
            description=description.strip(),
            status="pending",
            priority=priority,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Task":
        return Task(
            id=str(data["id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            status=str(data.get("status", "pending")),
            priority=str(data.get("priority", "normal")),
            created_at=str(data["created_at"]),
            updated_at=str(data.get("updated_at", data["created_at"])),
            metadata=dict(data.get("metadata", {})),
        )
