"""Cliente de las APIs de búsqueda/navegación de Ollama Cloud.

- web_search: POST https://ollama.com/api/web_search
- web_fetch:  POST https://ollama.com/api/web_fetch

Auth: header ``Authorization: Bearer <OLLAMA_CLOUD_API_KEY>``.
Docs: https://docs.ollama.com/web-search
"""

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("mallo.ollama_web")

BASE = "https://ollama.com/api"


def web_search(query: str, api_key: str, max_results: int = 5,
               timeout: float = 30.0) -> List[Dict[str, str]]:
    """Devuelve una lista de {title, url, content} para la consulta dada."""
    try:
        r = httpx.post(
            f"{BASE}/web_search",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "max_results": min(max(max_results, 1), 10)},
            timeout=timeout,
        )
        if r.status_code == 200:
            return r.json().get("results", [])
        logger.warning("web_search HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("web_search error: %s", e)
    return []


def web_fetch(url: str, api_key: str, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
    """Devuelve {title, content, links} de una URL, o None si falla."""
    try:
        r = httpx.post(
            f"{BASE}/web_fetch",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": url},
            timeout=timeout,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning("web_fetch HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("web_fetch error: %s", e)
    return None


def search_context(query: str, api_key: str, max_results: int = 5,
                   max_chars: int = 1800) -> str:
    """Resumen de texto plano de los resultados de búsqueda, para dar a un LLM."""
    results = web_search(query, api_key, max_results=max_results)
    parts = []
    for r in results:
        parts.append(f"- {r.get('title','')} ({r.get('url','')}): {r.get('content','')[:300]}")
    return "\n".join(parts)[:max_chars]
