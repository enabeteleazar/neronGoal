from __future__ import annotations

# Rapatrié (phase 4). DEFAULT_GENERATED_TOOLS reste PARTAGÉ avec le
# monolithe tant que tools/creator.py (qui écrit ici) n'a pas migré
# (phase 6).

import json
import threading
import unicodedata
from pathlib import Path

from server.common.paths import NERON_DATA_DIR, NERON_SERVER_DIR
from goal.infra.events import Event
from goal.infra.events import event_bus
from goal.infra.events import TOOL_REGISTERED
from goal.tools.models import ToolSpec


DEFAULT_REGISTRY_PATH = NERON_DATA_DIR / "tool_registry.json"
DEFAULT_GENERATED_TOOLS = NERON_SERVER_DIR / "tools" / "generated"


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.lower())
    normalized = "".join(
        char for char in normalized if unicodedata.category(char) != "Mn"
    )
    return " ".join(normalized.split())


class ToolRegistry:
    def __init__(
        self,
        path: Path | None = DEFAULT_REGISTRY_PATH,
        generated_dir: Path | None = DEFAULT_GENERATED_TOOLS,
    ) -> None:
        self.path = path
        self.generated_dir = generated_dir
        self._tools: dict[str, ToolSpec] = {}
        self._load()

    def register_tool(self, spec: ToolSpec) -> ToolSpec:
        handler_path = self._handler_path(spec)
        if handler_path and not Path(handler_path).is_file():
            raise ValueError(f"tool_handler_not_found:{handler_path}")
        self._tools[spec.slug] = spec
        self._save()
        event_bus.publish_background(Event(
            type=TOOL_REGISTERED,
            source="tool_registry",
            payload={
                "tool_slug": spec.slug,
                "source": spec.source,
                "handler_path": self._handler_path(spec),
            },
        ))
        return spec

    def get_tool(self, slug: str) -> ToolSpec | None:
        return self._tools.get(slug)

    def list_tools(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda spec: spec.slug)

    def find_tool_for_request(self, text: str) -> ToolSpec | None:
        query = _normalize(text)
        query_tokens = {token for token in query.split() if len(token) >= 3}
        best: tuple[float, ToolSpec] | None = None
        for spec in self._tools.values():
            aliases = list(spec.metadata.get("aliases") or [])
            haystack = _normalize(
                " ".join([spec.slug, spec.name, spec.description, *map(str, aliases)])
            )
            tokens = {token for token in haystack.split() if len(token) >= 3}
            overlap = len(query_tokens & tokens) / max(1, len(query_tokens))
            if overlap >= 0.34 and (best is None or overlap > best[0]):
                best = (overlap, spec)
        return best[1] if best else None

    def tool_exists(self, slug: str) -> bool:
        return slug in self._tools

    def diagnose_consistency(self) -> dict[str, object]:
        missing_handlers: list[dict[str, str]] = []
        workspace_backed: list[dict[str, str]] = []
        referenced_handlers: set[Path] = set()
        builtin_tools: list[str] = []
        for spec in self.list_tools():
            path = self._handler_path(spec)
            if not path:
                builtin_tools.append(spec.slug)
                continue
            resolved = Path(path)
            try:
                referenced_handlers.add(resolved.resolve())
            except OSError:
                pass
            if not resolved.is_file():
                missing_handlers.append({"tool": spec.slug, "path": path})
            if "workspace" in resolved.parts:
                workspace_backed.append({"tool": spec.slug, "path": path})
        orphan_generated_tools: list[str] = []
        if self.generated_dir is not None and self.generated_dir.is_dir():
            orphan_generated_tools = sorted(
                path.stem
                for path in self.generated_dir.glob("*.py")
                if path.is_file()
                and not path.name.startswith("_")
                and path.resolve() not in referenced_handlers
            )
        return {
            "registry_path": str(self.path) if self.path is not None else None,
            "generated_dir": (
                str(self.generated_dir) if self.generated_dir is not None else None
            ),
            "registered_tools": [spec.slug for spec in self.list_tools()],
            "builtin_tools": builtin_tools,
            "missing_handlers": missing_handlers,
            "workspace_backed_tools": workspace_backed,
            "orphan_generated_tools": orphan_generated_tools,
        }

    @staticmethod
    def _handler_path(spec: ToolSpec) -> str | None:
        value = spec.metadata.get("handler_path") or spec.metadata.get("tool_path")
        return str(value) if value else None

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = payload.get("tools", []) if isinstance(payload, dict) else []
        for item in items:
            if isinstance(item, dict):
                spec = ToolSpec.from_dict(item)
                if spec.slug:
                    self._tools[spec.slug] = spec

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tools": [spec.to_dict() for spec in self.list_tools()]}
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


_REGISTRY: ToolRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_tool_registry() -> ToolRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ToolRegistry()
        return _REGISTRY
