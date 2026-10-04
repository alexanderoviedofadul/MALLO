"""Cliente del ecosistema cedula360.

Dos usos:
1. Validar API keys entrantes (`c360_*`) contra `GET /api/usuario/me`.
2. Consumir datos oficiales colombianos (`GET /api/fuentes/all/{doc}`) para
   enriquecer las respuestas de MALLO.

Auth de cedula360: header ``X-API-Key`` (NO Bearer). Base canónica:
``https://cedula360.tech/api``.
"""

import logging
import time
from typing import Any, Dict, Optional

import httpx

from load_secrets import get_secret

logger = logging.getLogger("mallo.api.cedula360")

DEFAULT_BASE = "https://cedula360.tech/api"
# Cache simple de validación de keys: {key: (expira_ts, perfil)}
_VALIDATION_CACHE: Dict[str, tuple] = {}
_VALIDATION_TTL = 300  # 5 min


def base_url() -> str:
    return (get_secret("CEDULA360_API_BASE") or DEFAULT_BASE).rstrip("/")


def service_api_key() -> Optional[str]:
    """Key con la que MALLO consume cedula360 (no la del usuario que llama)."""
    return get_secret("CEDULA360_API_KEY")


def validate_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Valida una API key de cedula360. Devuelve el perfil si es válida, o None.

    Cachea resultados positivos durante _VALIDATION_TTL para no golpear cedula360
    en cada request.
    """
    now = time.time()
    cached = _VALIDATION_CACHE.get(api_key)
    if cached and cached[0] > now:
        return cached[1]

    try:
        resp = httpx.get(
            f"{base_url()}/usuario/me",
            headers={"X-API-Key": api_key},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Error validando key contra cedula360: %s", e)
        return None

    if resp.status_code == 200:
        data = resp.json()
        perfil = {
            "id": data.get("id"),
            "email": data.get("email"),
            "name": data.get("name"),
            "plan": data.get("plan"),
            "role": data.get("role"),
            "consultas_mes_actual": data.get("consultas_mes_actual"),
        }
        _VALIDATION_CACHE[api_key] = (now + _VALIDATION_TTL, perfil)
        return perfil
    return None


def consultar(documento: str, tipo: str = "CC", *, refresh: bool = False,
              enriquecer: bool = False, huella: bool = False,
              api_key: Optional[str] = None, timeout: float = 60.0) -> Dict[str, Any]:
    """Consulta el dossier consolidado de cedula360 para un documento.

    Usa la key de servicio de MALLO salvo que se pase ``api_key`` (p. ej. la del
    usuario que llama, para que la cuota se cargue a su cuenta).
    """
    key = api_key or service_api_key()
    if not key:
        return {"error": "CEDULA360_API_KEY no configurada en MALLO"}

    params = {"tipo": tipo}
    if refresh:
        params["refresh"] = "true"
    if enriquecer:
        params["enriquecer"] = "true"
    if huella:
        params["huella"] = "true"

    try:
        resp = httpx.get(
            f"{base_url()}/fuentes/all/{documento}",
            params=params,
            headers={"X-API-Key": key},
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Error consultando cedula360: %s", e)
        return {"error": f"Error consultando cedula360: {e}"}

    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 429:
        return {"error": "Cuota de cedula360 excedida", "status": 429,
                "detalle": _safe_json(resp)}
    if resp.status_code == 401:
        return {"error": "API key de cedula360 inválida", "status": 401}
    return {"error": f"cedula360 HTTP {resp.status_code}", "detalle": _safe_json(resp)}


def gating() -> Dict[str, Any]:
    """Tabla pública de fuentes pagas + ranking de planes (sin auth)."""
    try:
        resp = httpx.get(f"{base_url()}/fuentes/gating", timeout=10)
        return resp.json() if resp.status_code == 200 else {"error": f"HTTP {resp.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:300]
