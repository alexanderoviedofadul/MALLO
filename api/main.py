"""API REST de MALLO — MultiAgent LLM Orchestrator.

Expone la orquestación multi-agente como servicio HTTP, con autenticación
configurable (key propia de MALLO y/o key de cedula360) e integración con los
datos oficiales colombianos de cedula360.

Arranque:  uvicorn api.main:app --host 0.0.0.0 --port 9006
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import json

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api import cedula360_client
from api.auth import authenticate
from api.manager import get_manager, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("mallo.api")

API_VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializa el AgentManager en el arranque (no en la primera request).
    try:
        get_manager()
    except Exception as e:  # noqa: BLE001
        logger.error("Fallo inicializando AgentManager en startup: %s", e)
    yield


app = FastAPI(
    title="MALLO API",
    description="MultiAgent LLM Orchestrator — API REST con integración cedula360",
    version=API_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────── Schemas ───────────────────────────
class ChatRequest(BaseModel):
    query: str = Field(..., description="Consulta del usuario")
    provider: str = Field(..., description="Proveedor/agent_type (ej. ollama_cloud, groq, openrouter)")
    model: str = Field(..., description="Modelo a usar dentro del proveedor")
    prompt_type: Optional[str] = Field(None, description="Tipo de prompt opcional (coding, legal, ...)")


class OrchestrateRequest(BaseModel):
    query: str = Field(..., description="Consulta del usuario")
    prompt_type: Optional[str] = Field(None, description="Forzar tipo de prompt (opcional)")
    enrich_cedula360: Optional[Dict[str, str]] = Field(
        None,
        description="Opcional: {documento, tipo} para enriquecer con datos de cedula360",
    )


class ConsultaResponse(BaseModel):
    documento: str
    tipo: str
    resultado: Dict[str, Any]


# ─────────────────────────── Endpoints públicos ───────────────────────────
@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "mallo-api", "version": API_VERSION, "ts": time.time()}


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "MALLO API",
        "version": API_VERSION,
        "docs": "/docs",
        "endpoints": ["/health", "/v1/models", "/v1/chat", "/v1/orchestrate",
                      "/v1/cedula360/consulta/{documento}", "/v1/cedula360/gating"],
    }


# ─────────────────────────── Endpoints autenticados ───────────────────────────
@app.get("/v1/models")
def list_models(principal: Dict[str, Any] = Depends(authenticate)) -> Dict[str, Any]:
    """Lista los proveedores y modelos declarados en config.yaml."""
    config = load_config()
    providers: Dict[str, List[str]] = {}
    for api_name, section in config.items():
        if isinstance(section, dict) and "models" in section:
            models = []
            for m in section["models"]:
                if isinstance(m, dict) and "name" in m:
                    models.append(m["name"])
                elif isinstance(m, str):
                    models.append(m)
            if models:
                providers[api_name] = models
    return {"providers": providers, "count": sum(len(v) for v in providers.values())}


@app.post("/v1/chat")
def chat(req: ChatRequest, principal: Dict[str, Any] = Depends(authenticate)) -> Dict[str, Any]:
    """Procesa la consulta con un proveedor+modelo específicos (con fallback interno)."""
    mgr = get_manager()
    if req.provider not in ["local", "assistant"] and not mgr.clients.get(req.provider):
        raise HTTPException(status_code=400, detail=f"Proveedor '{req.provider}' no disponible/inicializado")
    start = time.time()
    try:
        response = mgr.process_query(req.query, req.provider, req.model, req.prompt_type)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Error procesando: {e}")
    if isinstance(response, dict) and "error" in response:
        raise HTTPException(status_code=502, detail=response["error"])
    return {
        "response": response,
        "provider": req.provider,
        "model": req.model,
        "elapsed_s": round(time.time() - start, 2),
        "principal": principal.get("principal"),
    }


@app.post("/v1/chat/stream")
def chat_stream(req: ChatRequest, principal: Dict[str, Any] = Depends(authenticate)):
    """Igual que /v1/chat pero con respuesta en streaming (SSE, text/event-stream).

    Usa el cliente OpenAI-compatible del proveedor con ``stream=True``. Para
    proveedores con SDK propio (anthropic, cohere) degrada a un único evento con
    la respuesta completa vía ``process_query``.
    Formato de cada línea: ``data: {"delta": "..."}`` y al final ``data: [DONE]``.
    """
    mgr = get_manager()
    if req.provider not in ["local", "assistant"] and not mgr.clients.get(req.provider):
        raise HTTPException(status_code=400, detail=f"Proveedor '{req.provider}' no disponible/inicializado")

    query = req.query
    if req.prompt_type and req.prompt_type in getattr(mgr, "critical_analysis_prompts", {}):
        query = f"{mgr.critical_analysis_prompts[req.prompt_type]}\n\n{query}"

    def event_stream():
        client = mgr.clients.get(req.provider)
        section = mgr.config.get(req.provider, {}) if isinstance(mgr.config.get(req.provider), dict) else {}
        max_tokens = section.get("max_tokens", mgr.config["general"]["max_tokens"])
        temperature = section.get("temperature", mgr.config["general"]["temperature"])
        try:
            # Camino OpenAI-compatible (la mayoría de proveedores)
            if client is not None and hasattr(client, "chat"):
                stream = client.chat.completions.create(
                    model=req.model,
                    messages=[{"role": "user", "content": query}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        yield f"data: {json.dumps({'delta': content}, ensure_ascii=False)}\n\n"
                        continue
                    # Razonamiento de modelos "thinking" → campo separado (el
                    # cliente puede ignorarlo o mostrarlo aparte).
                    reasoning = getattr(delta, "reasoning", None) or getattr(delta, "thinking", None)
                    if reasoning:
                        yield f"data: {json.dumps({'reasoning': reasoning}, ensure_ascii=False)}\n\n"
            else:
                raise RuntimeError("cliente no streamable")
        except Exception as e:  # noqa: BLE001 — fallback no-streaming
            logger.warning("Streaming no disponible para %s (%s); fallback completo", req.provider, e)
            try:
                full = mgr.process_query(query, req.provider, req.model, req.prompt_type)
                if isinstance(full, dict):
                    full = full.get("content") or full.get("error") or str(full)
                yield f"data: {json.dumps({'delta': full}, ensure_ascii=False)}\n\n"
            except Exception as e2:  # noqa: BLE001
                yield f"data: {json.dumps({'error': str(e2)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/orchestrate")
def orchestrate(req: OrchestrateRequest, principal: Dict[str, Any] = Depends(authenticate)) -> Dict[str, Any]:
    """Orquestación dinámica: evalúa complejidad/tipo y selecciona el mejor agente."""
    from utilities import evaluate_query_complexity, determine_prompt_type_and_capabilities

    mgr = get_manager()
    query = req.query
    cedula_context = None

    # Enriquecimiento opcional con datos de cedula360
    if req.enrich_cedula360 and req.enrich_cedula360.get("documento"):
        doc = req.enrich_cedula360["documento"]
        tipo = req.enrich_cedula360.get("tipo", "CC")
        c360_key = principal.get("c360_key")  # si el caller usó su propia key
        cedula_context = cedula360_client.consultar(doc, tipo, api_key=c360_key)
        if "error" not in cedula_context:
            query = (
                f"{req.query}\n\n[Contexto oficial de cedula360 para {tipo} {doc}]:\n"
                f"{_resumir_dossier(cedula_context)}"
            )

    try:
        complexity = evaluate_query_complexity(query)
        if isinstance(complexity, tuple):
            complexity = complexity[0]
    except Exception:  # noqa: BLE001
        complexity = 0.5
    prompt_type = req.prompt_type
    if not prompt_type:
        try:
            prompt_type, _ = determine_prompt_type_and_capabilities(query)
        except Exception:  # noqa: BLE001
            prompt_type = "general"

    prioritized = mgr.get_prioritized_agents(query, complexity, prompt_type)
    pairs = [(a, m) for a, m, _ in prioritized]
    start = time.time()
    try:
        response, meta = mgr.process_query_with_fallback(query, pairs)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Error en orquestación: {e}")

    out = {
        "response": response,
        "selected_agent": meta,
        "complexity": round(float(complexity), 3),
        "prompt_type": prompt_type,
        "candidates": [{"provider": a, "model": m, "name": n} for a, m, n in prioritized],
        "elapsed_s": round(time.time() - start, 2),
        "principal": principal.get("principal"),
    }
    if cedula_context is not None:
        out["cedula360_enriched"] = "error" not in cedula_context
    return out


# ─────────────────────────── Endpoints cedula360 ───────────────────────────
@app.get("/v1/cedula360/consulta/{documento}", response_model=ConsultaResponse)
def cedula360_consulta(
    documento: str,
    tipo: str = "CC",
    refresh: bool = False,
    enriquecer: bool = False,
    huella: bool = False,
    principal: Dict[str, Any] = Depends(authenticate),
) -> ConsultaResponse:
    """Consulta el dossier oficial de cedula360 para un documento.

    Si el caller se autenticó con su propia key de cedula360, la cuota se carga a
    su cuenta; si no, se usa la key de servicio de MALLO.
    """
    c360_key = principal.get("c360_key")
    resultado = cedula360_client.consultar(
        documento, tipo, refresh=refresh, enriquecer=enriquecer,
        huella=huella, api_key=c360_key,
    )
    if "error" in resultado:
        raise HTTPException(status_code=resultado.get("status", 502), detail=resultado["error"])
    return ConsultaResponse(documento=documento, tipo=tipo, resultado=resultado)


@app.get("/v1/cedula360/gating")
def cedula360_gating(principal: Dict[str, Any] = Depends(authenticate)) -> Dict[str, Any]:
    """Tabla de fuentes pagas + planes de cedula360."""
    return cedula360_client.gating()


# ─────────────────────────── Helpers ───────────────────────────
def _resumir_dossier(dossier: Dict[str, Any], max_chars: int = 1500) -> str:
    """Resumen compacto del dossier para inyectarlo como contexto al LLM."""
    partes = []
    ident = dossier.get("identidad") or {}
    if ident:
        partes.append("Identidad: " + ", ".join(
            f"{k}={v}" for k, v in ident.items() if v not in (None, "", [])
        ))
    if dossier.get("resumen_ejecutivo"):
        partes.append("Resumen: " + str(dossier["resumen_ejecutivo"]))
    for clave in ("rama_judicial", "antecedentes", "contratacion", "sanciones"):
        val = dossier.get(clave)
        if val:
            partes.append(f"{clave}: {str(val)[:300]}")
    texto = "\n".join(partes)
    return texto[:max_chars] if texto else "(sin datos relevantes)"
