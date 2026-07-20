"""Fabrique d'outils pour les agents — périmètre goal (rapatrié depuis tools/).

models, registry, spec_builder, templates (phase 4) + creator, runtime,
code_generator (phase 6). La génération de code IA passe par
OllamaToolCodeGenerator (server/common/llm_client), plus par Codex CLI —
task_type='code' est garanti local par le plancher de sécurité du
service llm (voir server/llm/core/router.py).
"""
