"""Génération de code d'agent via le service llm (task_type='code').

Remplace l'ancien flux Codex CLI (agents.factory.build_orchestrator
._run_codex_agent_generation), qui faisait confiance au CLI pour écrire
lui-même agent_file ET test_file sur disque (mode agentique), avec un
filet de rattrapage réactif (snapshot avant/après) pour détecter une
écriture parasite.

Ici : un seul appel HTTP, un seul fichier à écrire (l'agent). Le fichier
de test réutilise le template déterministe existant
(AgentBuildOrchestrator._write_agent_test, inchangé) plutôt que d'être
généré par le modèle — moins de surface, le test de validation n'est
jamais façonné par l'IA elle-même. Comme le modèle ne peut renvoyer que
du texte (jamais écrire de fichier), le filet de rattrapage par snapshot
devient structurellement inutile : rien d'autre que le fichier attendu
ne peut être touché.
"""
from __future__ import annotations

import logging

from server.common.llm_client import LLMClient, LLMClientError, get_llm_client
from goal.infra.llm_text import strip_code_fence

logger = logging.getLogger("goal.agents_factory.code_generator")

MAX_ATTEMPTS = 2


class OllamaAgentCodeGenerator:
    def __init__(self, *, client: LLMClient | None = None) -> None:
        self.client = client or get_llm_client()

    async def generate_agent_code(self, prompt: str, *, request_id: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = await self.client.generate_code(prompt, request_id=request_id)
            except LLMClientError as exc:
                last_error = exc
                logger.warning(
                    "llm_generation_failed request_id=%s attempt=%s/%s error=%s",
                    request_id, attempt, MAX_ATTEMPTS, exc,
                )
                continue
            code = strip_code_fence(raw)
            if code.strip():
                return code
            last_error = RuntimeError("llm_empty_response")
        raise RuntimeError(f"ollama_agent_generation_failed:{last_error}")
