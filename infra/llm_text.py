"""Petits utilitaires de traitement de texte pour les réponses de modèles.

Centralisé ici (au lieu d'une copie privée par générateur) car utilisé à
la fois par la génération de code d'outils (goal.tools.code_generator)
et d'agents (goal.agents_factory).
"""
from __future__ import annotations

import re


def strip_code_fence(value: str) -> str:
    """Retire un bloc ```python ... ``` ou ``` ... ``` s'il enveloppe tout
    le texte. Si le modèle n'a pas utilisé de fence, renvoie le texte tel
    quel (strip simple)."""
    text = value.strip()
    match = re.fullmatch(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text
