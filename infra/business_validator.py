from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

from server.common.paths import NERON_ROOT
from goal.infra.agent_sandbox import AgentSandbox

_GENERIC_RESPONSE_MARKERS = (
    "agent disponible pour",
    "je suis un agent",
    "reponse deterministe",
)
_INTERNAL_GENERIC_RESPONSE_MARKERS = (
    "demande traitee",
    *_GENERIC_RESPONSE_MARKERS,
)


class BusinessValidator:
    def __init__(
        self,
        *,
        python_executable: str | None = None,
        project_root: Path | None = None,
        timeout: int = 30,
        sandbox: AgentSandbox | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.project_root = (project_root or NERON_ROOT).resolve()
        self.timeout = timeout
        self.sandbox = sandbox or AgentSandbox(
            python_executable=self.python_executable,
            project_root=self.project_root,
            timeout=self.timeout,
        )

    def validate(
        self,
        agent_spec: dict[str, Any] | Any,
        agent_path: str | Path,
        original_goal: str,
        scenario: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = self._spec_dict(agent_spec)
        validation_context = dict(context or {})
        internal_capability = bool(
            validation_context.get("internal_capability_request")
        )
        selected_scenario = scenario or self.generate_scenario(
            spec,
            original_goal,
            context=validation_context,
        )
        expected = dict(selected_scenario.get("expected") or {})
        if internal_capability:
            expected["reject_generic_markers"] = list(
                _INTERNAL_GENERIC_RESPONSE_MARKERS
            )
            selected_scenario = {
                **selected_scenario,
                "expected": expected,
            }
        result = {
            "ok": False,
            "status": "failed",
            "scenario": selected_scenario,
            "actual_response": "",
            "expected": expected,
            "errors": [],
            "validation_context": {
                "internal_capability_request": internal_capability,
                "creation_type": validation_context.get("creation_type"),
                "capability_request_id": validation_context.get(
                    "capability_request_id"
                ),
            },
        }

        if expected.get("reliable_scenario") is False:
            result["errors"].append("reliable_business_scenario_required")
            return result

        path = Path(agent_path).resolve()
        if not path.is_file():
            result["errors"].append(f"agent_file_not_found: {path}")
            return result

        execution = self._execute_agent(path, str(selected_scenario.get("input") or original_goal))
        result["sandbox"] = execution.get("sandbox")
        if not execution.get("ok"):
            result["errors"].append(str(execution.get("error") or "agent_execution_failed"))
            return result

        raw = execution.get("result")
        response = self._response_text(raw)
        result["actual_response"] = response
        result["errors"].extend(self._evaluate(raw, response, expected))
        if not result["errors"]:
            result["ok"] = True
            result["status"] = "passed"
        return result

    def generate_scenario(
        self,
        agent_spec: dict[str, Any] | Any,
        original_goal: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = self._spec_dict(agent_spec)
        validation_context = dict(context or {})
        searchable = self._normalize(
            " ".join(
                [
                    original_goal,
                    str(spec.get("goal") or ""),
                    str(spec.get("name") or ""),
                    " ".join(str(item) for item in spec.get("capabilities") or []),
                ]
            )
        )

        if "paques" in searchable or "easter" in searchable:
            return {
                "name": "easter_2027",
                "input": "Quelle est la date de Pâques en 2027 ?",
                "expected": {
                    "contains_all": ["2027"],
                    "contains_any": ["28 mars", "2027-03-28"],
                },
                "fallback": False,
            }
        if "noel" in searchable or "christmas" in searchable:
            return {
                "name": "christmas_countdown",
                "input": "Combien de temps reste-t-il avant Noël ?",
                "expected": {
                    "contains_any": ["Noël", "25/12"],
                },
                "fallback": False,
            }
        if (
            "subnet" in searchable
            or "ipv4" in searchable
            or "reseau ip" in searchable
        ):
            return {
                "name": "ipv4_subnet",
                "input": "Calcule le réseau de 192.168.1.42/24",
                "expected": {
                    "contains_any": ["192.168.1.0", "réseau"],
                },
                "fallback": False,
            }
        if "log" in searchable or "journal" in searchable:
            return {
                "name": "neron_log_analysis",
                "input": "Analyse les logs Néron",
                "expected": {
                    "contains_any": ["erreur", "aucune erreur", "log"],
                    "reject_generic_markers": list(
                        _INTERNAL_GENERIC_RESPONSE_MARKERS
                    ),
                },
                "fallback": False,
            }
        if validation_context.get("internal_capability_request"):
            return {
                "name": "unsupported_internal_capability",
                "input": original_goal,
                "expected": {
                    "reliable_scenario": False,
                    "reject_generic_markers": list(
                        _INTERNAL_GENERIC_RESPONSE_MARKERS
                    ),
                },
                "fallback": False,
            }
        explicit_response = self._explicit_response_contract(original_goal)
        if explicit_response:
            return {
                "name": "explicit_response_contract",
                "input": original_goal,
                "expected": {
                    "contains_all": [explicit_response],
                    "reject_generic_markers": list(
                        _INTERNAL_GENERIC_RESPONSE_MARKERS
                    ),
                },
                "fallback": False,
            }
        return {
            "name": "generic_non_empty_response",
            "input": original_goal,
            "expected": {
                "non_empty": True,
                "reject_generic_markers": list(_GENERIC_RESPONSE_MARKERS),
            },
            "fallback": True,
        }

    def _execute_agent(self, agent_path: Path, prompt: str) -> dict[str, Any]:
        return self.sandbox.execute_agent(
            agent_path,
            prompt,
            timeout=self.timeout,
            name="business_validation",
        )

    def _evaluate(
        self,
        raw_result: Any,
        response: str,
        expected: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if isinstance(raw_result, dict):
            status = self._normalize(str(raw_result.get("status") or ""))
            if status and status not in {"ok", "success", "passed"}:
                errors.append(f"agent_status_not_ok: {raw_result.get('status')}")

        normalized_response = self._normalize(response)
        if expected.get("non_empty") and not normalized_response:
            errors.append("empty_business_response")

        missing_all = [
            value
            for value in expected.get("contains_all") or []
            if self._normalize(str(value)) not in normalized_response
        ]
        if missing_all:
            errors.append(f"missing_expected_values: {', '.join(map(str, missing_all))}")

        contains_any = list(expected.get("contains_any") or [])
        if contains_any and not any(
            self._normalize(str(value)) in normalized_response
            for value in contains_any
        ):
            errors.append(
                f"missing_any_expected_value: {', '.join(map(str, contains_any))}"
            )

        rejected = [
            marker
            for marker in expected.get("reject_generic_markers") or []
            if self._normalize(str(marker)) in normalized_response
        ]
        if rejected:
            errors.append(f"generic_response_rejected: {', '.join(map(str, rejected))}")
        return errors

    def _response_text(self, result: Any) -> str:
        if isinstance(result, dict):
            for key in ("response", "content", "result", "answer"):
                value = result.get(key)
                if value is not None:
                    return str(value).strip()
            return json.dumps(result, ensure_ascii=False, default=str)
        return str(result or "").strip()

    def _spec_dict(self, agent_spec: dict[str, Any] | Any) -> dict[str, Any]:
        if isinstance(agent_spec, dict):
            return agent_spec
        if hasattr(agent_spec, "to_dict"):
            return dict(agent_spec.to_dict())
        return {}

    def _normalize(self, value: str) -> str:
        text = unicodedata.normalize("NFKD", value.lower())
        text = "".join(char for char in text if unicodedata.category(char) != "Mn")
        return " ".join(text.split())

    def _explicit_response_contract(self, goal: str) -> str | None:
        import re

        match = re.search(
            r"\b(?:repond|répond|reply|returns?)\s+(?:avec\s+)?(.+?)\s*$",
            goal.strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        response = match.group(1).strip(" \t\r\n\"'«».,;:")
        return response or None
