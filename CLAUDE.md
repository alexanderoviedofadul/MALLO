# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Idioma

Este proyecto se desarrolla en español. Commits, documentación interna (`docs/DEV_NOTES.md`, `CHANGELOG.md`) y comunicación están en español.

## Comandos

```bash
# Ejecutar la app (Streamlit en http://localhost:8501)
streamlit run main.py

# Modo experimental (habilita MALLOEnhancer / process_user_input_experimental)
streamlit run main.py -- --experimental

# Instalar dependencias de producción
pip install -r requirements.txt

# Instalar dependencias de desarrollo (mypy, black, isort, flake8, pytest)
pip install -r requirements-dev.txt

# Modelos de spaCy (solo desarrollo local; NO en Streamlit Cloud)
python -m spacy download es_core_news_sm
python -m spacy download en_core_web_sm

# Commit + push automático a main (incluye validaciones pre-commit:
# claves API expuestas, archivos >50MB, conflictos de merge)
./commit-simple.sh "mensaje del commit"

# Validar y sincronizar modelos contra las APIs reales (detecta modelos 404/descontinuados)
python tools/validate_and_sync_models.py
```

No hay suite de tests formal en el repo (el `tests/` mencionado en el README no existe). Los scripts en `tools/` son utilidades de benchmarking de velocidad de modelos, no tests unitarios.

## Arquitectura

MALLO (MultiAgent LLM Orchestrator) es una app Streamlit que orquesta múltiples proveedores de LLM, seleccionando dinámicamente agentes según la complejidad y el tipo de la consulta. Todo el estado vive en `st.session_state`; no hay base de datos.

### Flujo de una consulta (de extremo a extremo)

1. **`main.py`** captura la entrada del usuario y orquesta el pipeline en `process_user_input()`.
2. **`utilities.py::evaluate_query_complexity()`** calcula un score de complejidad (0–1) y **`determine_prompt_type_and_capabilities()`** clasifica el dominio (math, coding, legal, etc.).
3. **`agents.py::AgentManager`** selecciona el agente apropiado vía `get_appropriate_agent()` / `AgentSelector` (similitud de embeddings contra `specialized_assistants`) o agentes generales priorizados.
4. Etapas opcionales configurables desde la UI: búsqueda web, meta-análisis (MOA), evaluación ética.
5. **`process_query()`** ejecuta contra el proveedor y **reintenta automáticamente con agentes de respaldo** (hasta 3 fallbacks) si la respuesta falla o es inválida.
6. La respuesta y todos los metadatos del proceso se muestran en pestañas y pueden exportarse a Markdown.

### Componentes núcleo

- **`agents.py`** — Corazón del sistema. `AgentManager` inicializa un cliente por proveedor en `self.clients` (solo si la API key existe), verifica qué modelos están disponibles (`verify_models()`), y expone un método `process_with_<provider>()` por cada proveedor. `process_query()` despacha por convención de nombres: `getattr(self, f"process_with_{agent_type}")`. `AgentSelector` hace la selección por similitud semántica.
- **`utilities.py`** — Evaluación de complejidad/tipo de prompt, búsqueda web multi-proveedor, caché de respuestas, checks `check_<provider>_api()`, logging.
- **`model_loader.py`** — Carga dinámica del catálogo de modelos desde OpenRouter, Groq y Ollama, con caché en disco (ver `is_cache_valid`/`save_to_cache`). Infiere especialidad y capacidades de cada modelo.
- **`document_processor.py`** — Procesamiento de archivos adjuntos y OCR vía Mistral (`process_document_with_mistral_ocr`), gestión del contexto de documentos.
- **`config_streamlit.py`** — `set_streamlit_page_config()` debe llamarse **antes que cualquier import de Streamlit** (ver primeras líneas de `main.py`).
- **`load_secrets.py`** — Carga de secretos desde múltiples rutas (ver abajo).

### Proveedores LLM integrados

OpenAI, Anthropic, Groq, Together, DeepInfra, DeepSeek, Mistral, Cohere, Ollama (local), OpenRouter, Z.AI (GLM-4.6), RouteLLM y **Ollama Cloud**. Cada uno tiene su sección en `config.yaml` y su `init_*_client()` + `process_with_*()` en `agents.py`. La mayoría usan clientes compatibles con la API de OpenAI (`_process_with_openai_like_client`).

- **Ollama Cloud** (`ollama_cloud` en config.yaml): endpoint OpenAI-compatible `https://ollama.com/v1`, secret `OLLAMA_CLOUD_API_KEY`. 35 modelos cloud grandes (gpt-oss, deepseek-v4, glm-5.x, kimi, minimax, qwen3-coder, gemma, nemotron, mistral-large-3…). Los modelos con "thinking mode" pueden devolver `content` vacío con el razonamiento en `reasoning`/`thinking`; `process_with_ollama_cloud` lo maneja. Check del panel: `check_ollama_cloud_api` en `utilities.py`.

- **MiniMax (vía cedula360)** (`minimax` en config.yaml): proveedor multimodal consumido a través de la API de cedula360 (`POST /api/minimax/chat`, modelo `MiniMax-M3`). MALLO autentica con `X-API-Key` (`CEDULA360_API_KEY`); el backend de cedula360 habla con MiniMax por loopback (`127.0.0.1:8765`). **Requiere plan Enterprise/Despachos** (otros planes reciben 403). `init_minimax_client` valida `/api/minimax/usage`; `process_with_minimax` hace el POST y extrae `stdout`. Check del panel: `check_minimax_api`. La API de cedula360 expone además image/video/speech/music/vision (no integrados como LLM de texto).

### Búsqueda web

`perform_web_search` (utilities.py) usa una cascada: **Ollama Cloud (`ollama_search` → `POST https://ollama.com/api/web_search`, método principal)** → **MiniMax (`minimax_search` → `POST /api/minimax/search` de cedula360)** → YOU → Tavily → DuckDuckGo. `tools/ollama_web.py` implementa `web_search`/`web_fetch`/`search_context` (reutilizados también por el sincronizador de modelos). Orden configurable en `config.yaml` → `utilities.web_search.search_methods`.

### Asistentes especializados (sin OpenAI → corpus jurídico colombiano)

Los 26 `specialized_assistants` de `config.yaml` eran OpenAI Assistants (`asst_*`). Ahora se sirven desde **`claude-for-legal-colombia`** (Mentor RAG) vía **`POST /api/legal/ask`** de cedula360, sin depender de OpenAI:
- `agents.py::process_with_assistant` ya **no usa `clients["openai"]`**: resuelve el área del asistente con `self.assistant_area_map` (cargado de `tools/assistants_map.yaml`) y llama `process_with_cedula360_legal(area, query)` → `POST {CEDULA360_API_BASE}/legal/ask` con `X-API-Key` (plan Business+). Fallback a área `general` si no hay OpenAI ni área.
- La **selección** de asistente (keywords + embeddings Mistral) no cambió.
- Mapeo experto→área en `tools/assistants_map.yaml`; cada área tiene un system prompt en el Mentor (`reproductor/services/llm_service.py::system_for_area`, áreas jurídicas con reglas estrictas y transversales con conocimiento general).
- Extracción de los prompts originales: `tools/extract_assistants.py` (requiere reactivar `OPENAI_API_KEY` temporalmente; OpenAI **no** permite descargar los archivos adjuntos de los Assistants). Salida: `tools/assistants_export.json`.

### Activar/desactivar proveedores

Un proveedor se **desactiva comentando su API key** en `.streamlit/secrets.toml` (p. ej. `#GROQ_API_KEY = ...`). `init_client` devuelve `None` si la key está ausente, así que el cliente no se inicializa y el proveedor queda como NO disponible (❌ en el panel "🔧 Sistema" de la UI y `400` en `/v1/chat`). Estado actual: **activos** `deepseek`, `mistral`, `ollama_cloud`; **comentados** openai, groq, deepinfra, anthropic, z-ai, cohere, openrouter, together.

**Embeddings (selección semántica de agentes):** `AgentSelector` usa embeddings para elegir asistentes especializados. Backend en cascada: **Cohere** (`embed-english-v3.0`) → **Mistral** (`mistral-embed`, 1024 dim) → si ninguno hay key, degrada a `get_fallback_agent()`. Por eso, aunque Cohere esté comentado, la selección semántica sigue funcionando vía Mistral. La selección de modelos por `prompt_type`/`specialty` (`get_prioritized_agents`) no depende de embeddings.

### Arranque rápido del AgentManager (`verify`)

`AgentManager.__init__(config, verify=True)`. Con `verify=True` (default, lo usa Streamlit) llama `verify_models()`, que prueba **cada** modelo con una llamada real → arranque de varios minutos. La API REST instancia con `verify=False` para arrancar en segundos; la selección usa `clients`/`config` y el fallback en tiempo de request cubre los modelos no disponibles. Nota: `verify_models()` solo recorre una lista fija de proveedores (no incluye `ollama_cloud`, `z-ai`, `routellm`, `ollama`).

## API REST (FastAPI) — servicio `mallo-api`

Además de la UI Streamlit, MALLO expone una API REST en `api/` (FastAPI), servida en `mallo.cedula360.tech` e integrada con el ecosistema **cedula360**.

- **Servicio:** `mallo-api.service` (systemd, enabled) → `uvicorn api.main:app` en `127.0.0.1:9006`. Corre con system python (`/usr/bin/python3`), no con el venv vacío.
- **Estructura `api/`:** `main.py` (endpoints), `auth.py` (auth configurable), `cedula360_client.py` (validación de keys + consumo de datos), `manager.py` (singleton de `AgentManager`, `verify=False`).
- **Endpoints:** `GET /health` (público), `GET /v1/models`, `POST /v1/chat` (provider+model fijos), `POST /v1/chat/stream` (SSE, `text/event-stream`), `POST /v1/orchestrate` (selección dinámica; opción `enrich_cedula360`), `GET /v1/cedula360/consulta/{doc}?tipo=CC`, `GET /v1/cedula360/gating`. Docs en `/docs`.
  - **Streaming SSE:** `/v1/chat/stream` usa el cliente OpenAI-compatible del proveedor con `stream=True`; emite `data: {"delta": "..."}` por token y `data: [DONE]` al final. El razonamiento de modelos "thinking" sale en eventos `{"reasoning": "..."}` separados del `delta`. Proveedores con SDK propio (anthropic, cohere) degradan a un único evento completo.
- **Auth** (header `X-API-Key` o `Authorization: Bearer`): configurable vía secret `MALLO_API_AUTH_MODE` = `both`|`mallo`|`cedula360`|`none`. Valida contra `MALLO_API_KEYS` (keys propias, lista separada por comas) y/o contra cedula360 (`GET https://cedula360.tech/api/usuario/me` con `X-API-Key`, cacheado 5 min).
- **Integración cedula360:** secrets `CEDULA360_API_KEY` (key de servicio con la que MALLO consume cedula360) y `CEDULA360_API_BASE` (default `https://cedula360.tech/api`). El consumo usa header `X-API-Key` (NO Bearer); endpoint de consulta `GET /api/fuentes/all/{doc}?tipo=CC`. Si el caller se autenticó con su propia key `c360_*`, la cuota se carga a su cuenta.

### Despliegue del subdominio `mallo.cedula360.tech`

- **DNS:** registro A `mallo` → `82.25.92.96` en la zona de `cedula360.tech` (gestionada en Hostinger; sin wildcard, cada subdominio es un A individual).
- **Caddy** (bloque al final de `/etc/caddy/Caddyfile`): rutea `/v1/*`, `/health`, `/docs*`, `/redoc`, `/openapi.json` → API `:9006`; el resto → Streamlit `:8501`. TLS automático (Let's Encrypt). Logs en `/var/log/caddy/mallo-cedula360.log`.
- La API también se sirve en **`mallollm.alexanderoviedofadul.dev`** (mismo routing `@api` → `:9006`, resto → Streamlit `:8501`, más `/webhook*` → `:9005`). Es decir, ambos subdominios exponen backend + frontend.
- Tras editar el Caddyfile: `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile` y `systemctl reload caddy`. Backups con convención `Caddyfile.bak.pre-<motivo>-<timestamp>`.

## `config.yaml`: la fuente de verdad

Archivo central (~57KB) que controla casi todo el comportamiento. Secciones clave:

- Una sección por proveedor (`openrouter`, `groq`, `openai`, etc.) con `default_model` y lista de `models` (cada modelo puede declarar `specialty` y `prompt_types`).
- `specialized_assistants` — asistentes con `id`, `name`, keywords, usados por `AgentSelector`.
- `evaluation_models` — modelos para `initial`, `meta_analysis`, etc. (formato `api` + `model`).
- `thresholds` — `moa_complexity`, `local_complexity`, `web_search_complexity` controlan cuándo escalar.
- `critical_analysis` — prompts especializados por tipo, aplicados con cierta `probability`.
- `processing_priority`, `moa`, `cache`, `usage_limits`, `ui`, `utilities`.

**IMPORTANTE:** Algunos modelos están hardcodeados en `agents.py` además de en `config.yaml` (ver `default_agent`, `backup_default_agent` en `__init__`, y el modelo de prueba/fallback en `init_openrouter_client`/`process_with_openrouter`). Al actualizar modelos, mantener ambos sincronizados — esta desincronización ha causado errores 404 recurrentes (ver commits recientes). Usar `tools/validate_and_sync_models.py` para detectar modelos descontinuados antes de hacer cambios.

### Flujo de orquestación (Ollama Cloud principal + cascadas del benchmark)

Todo el flujo usa **Ollama Cloud** como motor principal, con **DeepSeek** de fallback. La selección por etapa/tarea sale de un **benchmark** (`tools/benchmark_tasks.py` → `tools/benchmark_results.json`, mide latencia + calidad con LLM-judge gpt-oss:120b):
- `processing_priority` (config.yaml): `assistant` (expertos jurídicos vía `/api/legal` si aplican) → `ollama_cloud` → `deepseek` → … `get_prioritized_agents` reordena los candidatos por esta lista (función `_prio`), así una consulta general va a Ollama Cloud y una jurídica al experto.
- `default_agent` = `ollama_cloud/qwen3-coder:480b` (top all-around del benchmark ponderado por calidad); `backup_default_agent` = `deepseek/deepseek-v4-flash` (`agents.py`). `evaluation_models.final`/`meta_analysis` también usan `qwen3-coder:480b`; `initial` usa `gemma4:31b`. El judge del benchmark pondera calidad (0-100) sobre latencia (penalización suave 0.3×s).
- `evaluation_models` (initial/final/meta_analysis) y `moa` (aggregator + proposals) corren sobre Ollama Cloud + DeepSeek backup. `run_moa`/`run_multiple_models` ya **no** usan `together` (estaba hardcodeado); usan `moa.api`/`moa.proposal_models`.
- DeepSeek actualizado a `deepseek-v4-flash` (default) y `deepseek-v4-pro` (`deepseek-chat`/`reasoner` se deprecan 2026-07-24). `process_with_deepseek` maneja el "thinking mode" (content vacío → `reasoning_content`, margen de tokens).

Regenerar cascadas: `python3 tools/benchmark_tasks.py` y aplicar `benchmark_results.json` a `evaluation_models`/`moa`.

### Estado verificado de proveedores (2026-06-23)

Modelos de `config.yaml` actualizados a los vigentes según el catálogo en vivo de cada API. Estado de respuesta probado con el `default_model`:

- **Funcionando (9):** openai (`gpt-5-nano`), groq (`groq/compound`), together (`Llama-3.3-70B-Instruct-Turbo`), deepseek (`deepseek-chat`), mistral (`mistral-small-latest`), cohere (`command-a-03-2025`), z-ai (`glm-4.6`), openrouter (`google/gemma-4-31b-it:free`), ollama_cloud (`gpt-oss:120b`).
- **Requieren saldo/billing en la cuenta** (config correcto, devuelven 402/400): **deepinfra** (falta billing address), **anthropic** (saldo insuficiente; `claude-3-haiku-20240307` estaba descontinuado → ahora `claude-haiku-4-5-20251001`), **routellm** (requiere método de pago).
- **OpenRouter free rota disponibilidad:** el tier `:free` cambia y satura (429). El modelo de prueba/default y los de la lista son los verificados al 2026-06-23; revalidar con `tools/validate_and_sync_models.py` si dan 429/404.
- **Ollama local** (`ollama.alexanderoviedofadul.dev`): el endpoint responde metadata (`/api/tags`) pero la **generación** hace timeout — problema de recursos del host, no del config. 36 modelos instalados; en `config.yaml` solo los locales no-`:cloud`.

### Sincronización mensual de modelos (cron)

`tools/sync_models_monthly.py` mantiene `config.yaml` al día con los catálogos **en vivo** de cada proveedor: lista los modelos disponibles vía API, detecta nuevos/retirados, **clasifica los nuevos con `gpt-oss:120b` (Ollama Cloud) apoyándose en la API de web search de Ollama** (`tools/ollama_web.py` → `POST /api/web_search`, `/api/web_fetch`), y reescribe el config con `ruamel.yaml` (preserva comentarios).

- Uso manual: `python3 tools/sync_models_monthly.py --dry-run` (reporta), sin flag aplica, `--no-research` (sin web), `--only groq,mistral`.
- Salvaguardas: backup `config.yaml.bak.sync-<ts>`; si el listado de un proveedor falla/viene vacío **no se modifica**; nunca retira `default_model`/`fast_model` ni los modelos referenciados en `evaluation_models`/`moa`/`agent_selection` (función `collect_critical_models`); filtro `EXCLUDE` descarta no-chat (embeddings, TTS, imagen, OCR…); cap de `CAP_PER_PROVIDER` (40) modelos por proveedor; clasificación por lotes de 12. Log en `tools/sync_models.log`.
- **Cron:** `mallo-models-sync.timer` (systemd, mensual, día 1 ~04:35) → `mallo-models-sync.service` ejecuta el sync y reinicia `mallo-api`. Secret usado: `OLLAMA_CLOUD_API_KEY`.
- Nota: `config.yaml` tenía la clave raíz `utilities` **duplicada** (PyYAML usaba silenciosamente la última, ignorando `image_analysis`/`file_handling`/`ocr`); se consolidó en una sola sección superset (requisito para que `ruamel` cargue el archivo).

## Secretos y configuración

`load_secrets.py` busca `secrets.toml` en orden: rutas de Render, `/app/.streamlit`, `.`, `.streamlit/`, `/etc/secrets`, luego variables de entorno (`.env` y prefijo `MALLO_`). Las API keys nunca van en código ni en `config.yaml`. En local viven en `.streamlit/secrets.toml` (gitignored); en Streamlit Cloud, en Settings → Secrets.

Acceder siempre a secretos vía `get_secret("KEY")`, no `os.environ` directo.

## Despliegue

- **Streamlit Cloud** es el target principal. `runtime.txt` fija Python 3.11.9 (3.13 rompe dependencias). `requirements.txt` está deliberadamente sin herramientas de dev para evitar fallos de instalación (ver `DEPLOYMENT.md` y `FIX_STREAMLIT_CLOUD.md`). `packages.txt` declara dependencias de sistema (tesseract-ocr).
- **VPS con Caddy** (reverse proxy). El proyecto se sirve en el subdominio **`mallollm.alexanderoviedofadul.dev`** (bloque en `/etc/caddy/Caddyfile`):
  - App Streamlit → `localhost:8501` (con `flush_interval -1` para WebSockets/streaming).
  - Auto-deploy → `/webhook*` → `localhost:9005`. GitHub apunta aquí; los commits `test: webhook ...` son validaciones de ese pipeline. Por eso `commit-simple.sh` hace push directo a `main`.
  - Logs de acceso en `/var/log/caddy/mallollm.log`.
- `vercel.json` existe pero el despliegue real es Streamlit Cloud / VPS.

## Notas

- `legacy_code/` contiene versiones antiguas (cache_manager, mallo_enhancer) que NO están en el flujo activo salvo el modo `--experimental`.
- Logs en `mallo.log` (raíz). Niveles INFO/WARNING/ERROR.
- `docs/DEV_NOTES.md` documenta correcciones técnicas concretas (p. ej. el workaround del argumento `proxies` en clientes Groq/Anthropic en `init_client`). Consultarlo antes de tocar la inicialización de clientes o la búsqueda YOU.
