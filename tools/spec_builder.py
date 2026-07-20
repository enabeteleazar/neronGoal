from __future__ import annotations

import re
import unicodedata
from typing import Any

from goal.tools.models import ToolNeed, ToolSpec


ROLE_PLANS: dict[str, tuple[str, ...]] = {
    "analyze": ("reader", "analyzer", "summary"),
    "summarize": ("reader", "summary"),
    "count": ("reader", "counter"),
    "diagnose": ("status", "diagnosis", "summary"),
    "monitor": ("collector", "detector", "summary"),
    "notify": ("collector", "detector", "summary"),
    "calculate": ("calculator",),
    "compare": ("reader", "comparator", "summary"),
    "search": ("reader", "search",),
    "execute": ("processor",),
}


class ToolSpecBuilder:
    def build_specs_from_need(self, need: ToolNeed) -> list[ToolSpec]:
        names = self.infer_tool_names(need)
        contracts = self.infer_inputs_outputs(need)
        safety = self.infer_safety(need)
        roles = self._roles(need)
        specs: list[ToolSpec] = []
        for slug, role in zip(names, roles, strict=True):
            inputs, outputs = contracts[role]
            specs.append(
                ToolSpec(
                    name=self._display_name(need.domain, role),
                    slug=slug,
                    description=(
                        f"Tool déterministe {role} pour le domaine "
                        f"{need.domain}; travaille uniquement sur le payload fourni."
                    ),
                    inputs=inputs,
                    outputs=outputs,
                    safety=dict(safety),
                    source="tool_creator_deterministic",
                    metadata={
                        "domain": need.domain,
                        "intent": need.intent,
                        "role": role,
                        "capability_goal": need.capability_goal,
                        "aliases": [need.request_text],
                        "constraints": list(need.constraints),
                        "generated": True,
                        "required_by_capability": need.metadata.get(
                            "matched_capability"
                        ),
                    },
                )
            )
        return specs

    def infer_tool_names(self, need: ToolNeed) -> list[str]:
        requested = need.metadata.get("required_tool_slugs")
        if isinstance(requested, list) and requested:
            return [self._safe_slug(str(slug)) for slug in requested]
        prefix = self._domain_prefix(need.domain)
        return [f"{prefix}_{role}_tool" for role in self._roles(need)]

    def infer_inputs_outputs(
        self,
        need: ToolNeed,
    ) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
        contracts: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for role in self._roles(need):
            inputs: dict[str, Any] = {
                "items": "list|optional",
                "data": "any|optional",
                "text": "string|optional",
            }
            outputs = self._outputs_for_role(role)
            for name in need.expected_inputs:
                inputs.setdefault(str(name), "any|optional")
            for name in need.expected_outputs:
                outputs.setdefault(str(name), "any")
            contracts[role] = (inputs, outputs)
        return contracts

    def infer_safety(self, need: ToolNeed) -> dict[str, Any]:
        return {
            "level": need.safety_level,
            "allow_system_commands": False,
            "network_access": False,
            "filesystem_access": False,
            "payload_only": True,
        }

    def _roles(self, need: ToolNeed) -> tuple[str, ...]:
        requested = need.metadata.get("required_tool_slugs")
        if isinstance(requested, list) and requested:
            return tuple(self._role_from_slug(str(slug)) for slug in requested)
        return ROLE_PLANS.get(need.intent, ("processor",))

    def _outputs_for_role(self, role: str) -> dict[str, Any]:
        if role in {"reader", "collector", "status"}:
            return {"entries": "list", "count": "integer", "status": "string"}
        if role in {"analyzer", "diagnosis", "detector"}:
            return {"issues": "list", "issue_count": "integer", "status": "string"}
        if role == "summary":
            return {
                "status": "string",
                "summary": "string",
                "issues": "list",
                "recommendation": "string",
            }
        if role == "counter":
            return {"count": "integer", "status": "string"}
        if role == "calculator":
            return {"value": "number|string", "status": "string"}
        if role == "comparator":
            return {"differences": "list", "count": "integer", "status": "string"}
        if role == "search":
            return {"matches": "list", "count": "integer", "status": "string"}
        return {"status": "string", "result": "any"}

    def _domain_prefix(self, domain: str) -> str:
        normalized = self._safe_slug(domain or "generic")
        if normalized.endswith("ies"):
            normalized = f"{normalized[:-3]}y"
        elif normalized.endswith("s") and not normalized.endswith("ss"):
            normalized = normalized[:-1]
        return normalized or "generic"

    def _role_from_slug(self, slug: str) -> str:
        normalized = self._safe_slug(slug)
        for role in {
            role
            for roles in ROLE_PLANS.values()
            for role in roles
        }:
            if f"_{role}_" in f"_{normalized}_":
                return role
        if "integrity" in normalized:
            return "analyzer"
        if "failure" in normalized:
            return "detector"
        return "processor"

    def _safe_slug(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.lower())
        normalized = "".join(
            char
            for char in normalized
            if unicodedata.category(char) != "Mn"
        )
        return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")

    def _display_name(self, domain: str, role: str) -> str:
        return f"{domain.title()} {role.replace('_', ' ').title()}"
