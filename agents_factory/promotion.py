from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from goal.agents_factory.registry import DEFAULT_GENERATED_AGENTS


class AgentPromotionService:
    def __init__(
        self,
        *,
        generated_dir: Path | str = DEFAULT_GENERATED_AGENTS,
        runtime_governor: Any | None = None,
    ) -> None:
        self.generated_dir = Path(generated_dir)
        self.runtime_governor = runtime_governor

    def promote(
        self,
        source: Path | str,
        *,
        requested_by: str,
    ) -> dict[str, Any]:
        path = Path(source)
        if not path.is_file():
            return {"ok": False, "error": "agent_not_found"}

        governor = self.runtime_governor or self._get_runtime_governor()
        allowed = governor.authorize_agent_promotion(
            agent_name=path.stem,
            requested_by=requested_by,
        )
        policy = governor.to_dict()
        if not allowed:
            return {
                "ok": False,
                "error": "runtime_governor_refused",
                "governor_status": "refused",
                "governor_policy": policy,
            }

        self.generated_dir.mkdir(parents=True, exist_ok=True)
        destination = self.generated_dir / path.name
        shutil.copy2(path, destination)
        return {
            "ok": True,
            "source": str(path),
            "destination": str(destination),
            "governor_status": "allowed",
            "governor_policy": policy,
        }

    @staticmethod
    def _get_runtime_governor() -> Any:
        from core.runtime.governor import get_runtime_governor

        return get_runtime_governor()
