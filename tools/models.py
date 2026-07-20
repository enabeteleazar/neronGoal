from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolNeed:
    request_text: str
    domain: str
    intent: str
    capability_goal: str
    expected_inputs: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    safety_level: str = "low"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolSpec:
    name: str
    slug: str
    description: str
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    source: str = "tool_creator"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ToolSpec":
        return cls(
            name=str(payload.get("name") or ""),
            slug=str(payload.get("slug") or ""),
            description=str(payload.get("description") or ""),
            inputs=dict(payload.get("inputs") or {}),
            outputs=dict(payload.get("outputs") or {}),
            safety=dict(payload.get("safety") or {}),
            source=str(payload.get("source") or "tool_creator"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class ToolResult:
    ok: bool
    response: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
