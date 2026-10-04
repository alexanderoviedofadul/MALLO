"""Autenticación configurable para la API de MALLO.

Modos (secret ``MALLO_API_AUTH_MODE``):
  - ``both``      → acepta key propia de MALLO **o** key de cedula360 (default).
  - ``mallo``     → solo keys propias de MALLO (lista en ``MALLO_API_KEYS``).
  - ``cedula360`` → solo keys de cedula360 (validadas contra su API).
  - ``none``      → sin autenticación (no recomendado).

La key se envía en el header ``X-API-Key`` o ``Authorization: Bearer <key>``.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, status

from load_secrets import get_secret
from api import cedula360_client

logger = logging.getLogger("mallo.api.auth")


def _mallo_keys() -> set:
    raw = get_secret("MALLO_API_KEYS") or ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _mode() -> str:
    return (get_secret("MALLO_API_AUTH_MODE") or "both").strip().lower()


def _extract_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def authenticate(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Dependencia FastAPI. Devuelve el principal autenticado o lanza 401."""
    mode = _mode()
    if mode == "none":
        return {"source": "none", "principal": "anonymous"}

    key = _extract_key(x_api_key, authorization)
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta API key (header X-API-Key o Authorization: Bearer).",
            headers={"WWW-Authenticate": "X-API-Key"},
        )

    # 1) Key propia de MALLO
    if mode in ("both", "mallo") and key in _mallo_keys():
        return {"source": "mallo", "principal": "mallo-key", "key_prefix": key[:12]}

    # 2) Key de cedula360 (validada contra su API)
    if mode in ("both", "cedula360") and key.startswith("c360_"):
        perfil = cedula360_client.validate_key(key)
        if perfil:
            return {
                "source": "cedula360",
                "principal": perfil.get("email"),
                "plan": perfil.get("plan"),
                "role": perfil.get("role"),
                "c360_key": key,  # se reutiliza para cargar la cuota al usuario
            }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key inválida.",
        headers={"WWW-Authenticate": "X-API-Key"},
    )
