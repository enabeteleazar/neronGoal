from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import time
import uuid


@dataclass
class Goal:
    id: str
    title: str
    description: str = ""
    priority: str = "medium"
    status: str = "pending"
    current_step: str = "pending"
    source: str = "system"
    progress: float = 0.0
    error: str | None = None
    dependencies: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        title: str,
        description: str = "",
        priority: str = "medium",
        source: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> "Goal":
        return cls(
            id=f"goal_{uuid.uuid4().hex[:12]}",
            title=title,
            description=description,
            priority=priority,
            source=source,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "current_step": self.current_step,
            "source": self.source,
            "progress": self.progress,
            "error": self.error,
            "dependencies": self.dependencies,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Goal":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            priority=data.get("priority", "medium"),
            status=data.get("status", "pending"),
            current_step=data.get("current_step", data.get("status", "pending")),
            source=data.get("source", "system"),
            progress=float(data.get("progress", 0.0)),
            error=data.get("error"),
            dependencies=list(data.get("dependencies", [])),
            metadata=dict(data.get("metadata", {})),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )
