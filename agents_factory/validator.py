from __future__ import annotations

import py_compile
from pathlib import Path


def validate_agent(path: str) -> dict:
    p = Path(path)

    if not p.exists():
        return {
            "ok": False,
            "error": "fichier introuvable",
        }

    try:
        py_compile.compile(str(p), doraise=True)
    except Exception as e:
        return {
            "ok": False,
            "error": f"syntax_error: {e}",
        }

    content = p.read_text(encoding="utf-8")

    if "class Agent" not in content:
        return {
            "ok": False,
            "error": "class Agent manquante",
        }

    if "async def execute" not in content:
        return {
            "ok": False,
            "error": "méthode execute manquante",
        }

    return {
        "ok": True,
    }
