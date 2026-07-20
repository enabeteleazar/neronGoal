"""Authentification du service goal.

Convention cluster : Authorization: Bearer $NERON_API_KEY.
Clé absente = mode ouvert (comportement historique), signalé au démarrage.
"""
from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status


def expected_api_key() -> str:
    return os.getenv("NERON_API_KEY", "").strip()


async def require_api_key(request: Request) -> None:
    expected = expected_api_key()
    if not expected:
        return
    header = request.headers.get("Authorization", "")
    provided = header.removeprefix("Bearer ").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
