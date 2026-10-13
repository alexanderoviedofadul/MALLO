#!/usr/bin/env python3
"""Sincronizador mensual de modelos de MALLO.

Mantiene `config.yaml` al día con los catálogos EN VIVO de cada proveedor:
agrega modelos nuevos (clasificados por un LLM de Ollama Cloud apoyado en su
API de web search) y retira los descontinuados, de forma conservadora y segura.

Uso:
  python3 tools/sync_models_monthly.py --dry-run     # solo reporta, no escribe
  python3 tools/sync_models_monthly.py               # aplica cambios + backup
  python3 tools/sync_models_monthly.py --no-research  # sin web_search (más rápido)
  python3 tools/sync_models_monthly.py --only groq,mistral

Salvaguardas:
  - Backup de config.yaml antes de escribir, en /root/backups/MALLO/config-bak/
    (fuera del árbol del servicio y del repo).
  - Si el listado de un proveedor falla o viene vacío -> NO se modifica.
  - Ollama Cloud se lee del catálogo público vigente (https://ollama.com/api/tags);
    el Ollama local, de los modelos con pesos en este servidor: los alias de nube
    (`:cloud`, `-cloud`, `remote_host`, p. ej. bladealex/*) no cuentan como locales.
  - Los proveedores de `disabled_providers` no se listan ni se clasifican.
  - Ninguna referencia del flujo (evaluation_models, moa, task_models,
    fallback_agents, summary_models, prompt/agent_selection, fast_model) se queda
    apuntando a un modelo retirado: se cambia por el default_model de su
    proveedor y se registra ALERTA en tools/sync_models.log.
  - Mistral: un modelo por grupo de alias; duplicados exactos fuera.
  - Cap de modelos por proveedor (evita catálogos enormes).
  - ruamel.yaml preserva comentarios y formato del resto del archivo.
"""

import argparse
import datetime
import json
import logging
import os
import re
import shutil
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from load_secrets import load_secrets, get_secret  # noqa: E402
load_secrets()
from tools import ollama_web  # noqa: E402

from ruamel.yaml import YAML  # noqa: E402
from ruamel.yaml.comments import CommentedMap, CommentedSeq  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")
# Las copias van FUERA del árbol que lee el servicio (y del repo).
BACKUP_DIR = "/root/backups/MALLO/config-bak"
# Catálogo vigente de Ollama Cloud (público) y Ollama local de este servidor.
OLLAMA_CLOUD_CATALOG = "https://ollama.com/api/tags"
OLLAMA_LOCAL = "http://127.0.0.1:11434"
LOG_PATH = os.path.join(ROOT, "tools", "sync_models.log")

# force=True: load_secrets/agents configuran logging antes y, sin él, basicConfig
# no hacía nada (sync_models.log llevaba en 0 bytes desde junio).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    force=True,
)
log = logging.getLogger("sync_models")

OLLAMA_KEY = get_secret("OLLAMA_CLOUD_API_KEY")
CLASSIFIER_MODEL = "gpt-oss:120b"
CAP_PER_PROVIDER = 40

VALID_SPECIALTIES = ["general", "coding", "multimodal"]
VALID_PROMPT_TYPES = [
    "general", "scientific", "historical", "philosophical", "ethical", "cultural",
    "political", "economic", "analytical", "coding", "math", "legal", "multimodal", "visual",
]

# Modelos NO conversacionales que nunca deben entrar al config de chat
# (embeddings, TTS/ASR, imagen/video, moderación, rerank, OCR, etc.).
EXCLUDE = re.compile(
    r"(embed|sentence-transformers|intfloat|\bbaai\b|thenlper|shibing|jina-emb|"
    r"nomic|gte-|bge-|e5-|minilm|mpnet|clip-vit|multi-qa|paraphrase|mxbai|"
    r"whisper|tts|text2vec|rerank|moderation|guard|safeguard|content-safety|"
    r"\bocr\b|image|img|video|veo|flux|sdxl|seedance|seedream|wan2|bria|kokoro|"
    r"voxtral|transcribe|realtime|asr|orpheus|\bcsm\b|diffus|redux|kontext|"
    r"pixverse|cosmos|chatterbox|higgs|leanstral|prompt-guard|fim|vector|"
    r"-tts|nemo-guard|aya-vision|qwen-image|qwen3-tts|p-image|p-video|"
    r"blur|erase|expand|remove_background|fibo|vibe-cli|-cli-|code-fim)",
    re.I,
)


# ───────────────────────── Listado de proveedores en vivo ─────────────────────────
ALIAS_GROUPS = {}  # id -> {id y sus alias} (solo proveedores que los publican)

def list_provider_models(mgr, provider: str):
    """Devuelve la lista de IDs de modelos en vivo del proveedor, o None si falla."""
    try:
        if provider in ("openai", "groq", "deepinfra", "deepseek", "z-ai"):
            c = mgr.clients.get(provider) or (mgr.clients.get("api") if provider == "openai" else None)
            if not c:
                return None
            return [m.id for m in c.models.list().data]
        if provider == "together":
            ids = [m.id for m in mgr.clients["together"].models.list()]
            return ids or None
        if provider == "mistral":
            # Mistral publica cada modelo con varios alias (mistral-large-latest =
            # mistral-large-2512…). Se devuelven todos (para no retirar alias en
            # uso) y se apuntan los grupos para no AÑADIR dos nombres del mismo.
            ids = []
            for m in mgr.clients["mistral"].models.list().data:
                ALIAS_GROUPS[m.id] = {m.id, *(getattr(m, "aliases", None) or [])}
                ids.append(m.id)
            return ids
        if provider == "cohere":
            return [m.name for m in mgr.clients["cohere"].models.list().models]
        if provider == "anthropic":
            return [m.id for m in mgr.clients["anthropic"].models.list().data]
        if provider == "ollama_cloud":
            # Catálogo vigente: lo que ollama.com publica hoy. Un modelo retirado
            # desaparece de aquí aunque su nombre siga respondiendo como alias.
            r = httpx.get(OLLAMA_CLOUD_CATALOG, timeout=30)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])] or None
        if provider == "openrouter":
            key = get_secret("OPENROUTER_API_KEY")
            r = httpx.get("https://openrouter.ai/api/v1/models",
                          headers={"Authorization": f"Bearer {key}"}, timeout=30)
            return [m["id"] for m in r.json().get("data", []) if m["id"].endswith(":free")]
        if provider == "ollama":
            return list_local_ollama()
    except Exception as e:  # noqa: BLE001
        log.warning("Listado de %s falló: %s", provider, e)
        return None
    return None


def list_local_ollama():
    """Modelos con pesos EN ESTE servidor. Los alias de nube (`:cloud`, `-cloud`
    y los que el API marca con `remote_host`, como los bladealex/* sustitutos)
    NO son locales aunque aparezcan en `ollama list`. Los modelos con espacio de
    nombres (usuario/modelo) son personas de otros proyectos y no entran."""
    try:
        r = httpx.get(f"{OLLAMA_LOCAL}/api/tags", timeout=15)
        r.raise_for_status()
        out = []
        for m in r.json().get("models", []):
            name = m.get("name", "")
            if m.get("remote_host") or m.get("remote_model"):
                continue
            if name.endswith(":cloud") or name.endswith("-cloud") or "/" in name:
                continue
            if (m.get("size") or 0) < 50_000_000:  # un alias remoto pesa unos cientos de bytes
                continue
            out.append(name)
        return out or None
    except Exception as e:  # noqa: BLE001
        log.warning("Ollama local no responde (%s); se intenta con `ollama list`", e)
    try:
        import subprocess
        txt = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=30).stdout
        out = []
        for line in txt.splitlines()[1:]:
            cols = line.split()
            if len(cols) >= 3 and cols[2] != "-" and "cloud" not in cols[0] and "/" not in cols[0]:
                out.append(cols[0])
        return out or None
    except Exception as e:  # noqa: BLE001
        log.warning("`ollama list` falló: %s", e)
        return None


def is_chat_model(name: str) -> bool:
    return not EXCLUDE.search(name)


# ───────────────────────── Clasificación con LLM + web ─────────────────────────
def _classify_batch(provider, batch, web_ctx):
    prompt = f"""Eres un catalogador de modelos LLM. Clasifica cada modelo del proveedor "{provider}".

Devuelve EXCLUSIVAMENTE un JSON (sin texto extra, sin markdown) con forma:
[{{"name": "...", "specialty": "...", "capabilities": ["..."], "prompt_types": ["..."]}}]

Reglas:
- "specialty" debe ser uno de: {VALID_SPECIALTIES}
- "prompt_types" debe ser un subconjunto de: {VALID_PROMPT_TYPES}
- "capabilities": 2-5 etiquetas cortas en snake_case (advanced_reasoning, code_generation,
  vision_understanding, multilingual_support, efficient_inference, large_context, thinking_mode, agentic, tool_use).
- Usa el nombre como pista (coder->coding, vl/vision/image->multimodal, mini/nano->efficient_inference).

Modelos:
{json.dumps(batch, ensure_ascii=False)}

Contexto web (puede ayudar):
{web_ctx or '(sin contexto)'}
"""
    try:
        r = httpx.post(
            "https://ollama.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OLLAMA_KEY}", "Content-Type": "application/json"},
            json={"model": CLASSIFIER_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": 3000},
            timeout=180,
        )
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        log.warning("Clasificación LLM falló (%s): %s", provider, e)
        return {}
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for item in arr:
        name = item.get("name")
        if not name:
            continue
        spec = item.get("specialty") if item.get("specialty") in VALID_SPECIALTIES else "general"
        caps = [c for c in (item.get("capabilities") or []) if isinstance(c, str)][:5] or ["instruction_following"]
        pts = [p for p in (item.get("prompt_types") or []) if p in VALID_PROMPT_TYPES] or ["general"]
        out[name] = {"specialty": spec, "capabilities": caps, "prompt_types": pts}
    return out


def classify_new_models(provider, new_models, research=True):
    """Clasifica los modelos nuevos por lotes. Devuelve {name: {specialty,...}}."""
    if not new_models:
        return {}
    web_ctx = ""
    if research:
        q = f"{provider} AI language models 2026 capabilities: " + ", ".join(new_models[:8])
        web_ctx = ollama_web.search_context(q, OLLAMA_KEY, max_results=5)

    out = {}
    BATCH = 12
    for i in range(0, len(new_models), BATCH):
        out.update(_classify_batch(provider, new_models[i:i + BATCH], web_ctx))
    # Completar los que el LLM no devolvió con un default seguro
    for name in new_models:
        out.setdefault(name, {"specialty": "general",
                              "capabilities": ["instruction_following"],
                              "prompt_types": ["general"]})
    return out


# ───────────────────────── Diff + actualización del config ─────────────────────────
def make_model_map(name, cls):
    cm = CommentedMap()
    cm["name"] = name
    cm["specialty"] = cls["specialty"]
    cm["capabilities"] = CommentedSeq(cls["capabilities"])
    cm["capabilities"].fa.set_flow_style()
    cm["prompt_types"] = CommentedSeq(cls["prompt_types"])
    cm["prompt_types"].fa.set_flow_style()
    return cm


def _api_model(ref):
    api, _, model = str(ref).partition(":")
    return (api, model) if model else None


def iter_references(cfg):
    """Todas las referencias (api, modelo) que el flujo usa fuera de las listas
    de proveedores, con un setter para poder repararlas. Devuelve tuplas
    (descripcion, api, modelo, setter)."""
    refs = []
    ev = cfg.get("evaluation_models", {}) or {}
    for stage, conf in ev.items():
        if not isinstance(conf, dict):
            continue
        for ak, mk in (("api", "model"), ("backup_api", "backup_model"), ("backup_api2", "backup_model2")):
            if conf.get(ak) and conf.get(mk):
                refs.append((f"evaluation_models.{stage}.{mk}", conf[ak], conf[mk],
                             (lambda v, c=conf, k=mk: c.__setitem__(k, v))))
    moa = cfg.get("moa", {}) or {}
    if moa.get("aggregator_model"):
        refs.append(("moa.aggregator_model", moa.get("api", "ollama_cloud"), moa["aggregator_model"],
                     (lambda v, c=moa: c.__setitem__("aggregator_model", v))))
    for i, m in enumerate(moa.get("proposal_models", []) or []):
        refs.append((f"moa.proposal_models[{i}]", moa.get("api", "ollama_cloud"), m,
                     (lambda v, seq=moa["proposal_models"], j=i: seq.__setitem__(j, v))))
    for clave in ("task_models",):
        for tarea, seq in (cfg.get(clave, {}) or {}).items():
            for i, ref in enumerate(seq or []):
                am = _api_model(ref)
                if am and am[0] != "assistant":
                    refs.append((f"{clave}.{tarea}[{i}]", am[0], am[1],
                                 (lambda v, s=seq, j=i, a=am[0]: s.__setitem__(j, f"{a}:{v}"))))
    for clave in ("fallback_agents", "summary_models"):
        seq = cfg.get(clave, []) or []
        for i, ref in enumerate(seq):
            am = _api_model(ref)
            if am and am[0] not in ("local", "assistant"):
                refs.append((f"{clave}[{i}]", am[0], am[1],
                             (lambda v, s=seq, j=i, a=am[0]: s.__setitem__(j, f"{a}:{v}"))))
    for sec in ("prompt_selection", "agent_selection"):
        conf = cfg.get(sec, {}) or {}
        for k in ("default_model", "validation_model"):
            am = _api_model(conf.get(k, "")) if conf.get(k) else None
            if am:
                refs.append((f"{sec}.{k}", am[0], am[1],
                             (lambda v, c=conf, kk=k, a=am[0]: c.__setitem__(kk, f"{a}:{v}"))))
    return refs


def collect_critical_models(cfg):
    """{proveedor: {modelos}} que el flujo usa fuera de las listas (no se retiran
    mientras sigan vigentes; si dejan de estarlo, `reparar_referencias` los cambia)."""
    crit = {}
    for _, api, model, _ in iter_references(cfg):
        crit.setdefault(api, set()).add(model)
    return crit


def reparar_referencias(cfg, vigentes, dry_run=False):
    """Toda referencia a un modelo que ya no está en el catálogo vivo de su
    proveedor se cambia por el default_model de ese proveedor (que sí lo está).
    Así un retiro no deja el flujo apuntando a un modelo muerto hasta que alguien
    lo note. Devuelve la lista de cambios."""
    cambios = []
    for desc, api, model, setter in iter_references(cfg):
        live = vigentes.get(api)
        if not live or model in live:
            continue
        sustituto = (cfg.get(api, {}) or {}).get("default_model")
        if not sustituto or sustituto not in live:
            log.warning("ALERTA %s → %s:%s retirado y sin sustituto vigente", desc, api, model)
            continue
        log.warning("ALERTA %s: %s:%s ya no está vigente → %s", desc, api, model, sustituto)
        cambios.append((desc, model, sustituto))
        if not dry_run:
            setter(sustituto)
    for sec, k in (("ollama_cloud", "fast_model"), ("ollama", "default_model")):
        conf = cfg.get(sec, {}) or {}
        live = vigentes.get(sec)
        if live and conf.get(k) and conf[k] not in live:
            nuevo = conf.get("default_model") if conf.get("default_model") in live else live[0]
            log.warning("ALERTA %s.%s: %s ya no está → %s", sec, k, conf[k], nuevo)
            cambios.append((f"{sec}.{k}", conf[k], nuevo))
            if not dry_run:
                conf[k] = nuevo
    return cambios


def sync_provider(cfg, provider, live_ids, research, dry_run, extra_protected=None):
    """Aplica el diff de un proveedor sobre cfg (ruamel). Devuelve dict de cambios."""
    section = cfg.get(provider)
    if not isinstance(section, dict) or "models" not in section:
        return None
    live_chat = [m for m in live_ids if is_chat_model(m)]
    if not live_chat:
        log.info("[%s] sin modelos de chat en el listado -> se omite", provider)
        return None

    current = []
    duplicados = []
    for m in section["models"]:
        if isinstance(m, dict) and "name" in m:
            if m["name"] in current:
                duplicados.append(m["name"])
            current.append(m["name"])
    current_set = set(current)
    live_set = set(live_chat)

    added_all = []
    ya = set(current_set)
    for m in live_chat:
        grupo = ALIAS_GROUPS.get(m, {m})
        if grupo & ya:
            continue  # ya está ese modelo con otro nombre
        added_all.append(m)
        ya |= grupo
    removed = [m for m in current if m not in live_set]

    # Proteger defaults y referencias del flujo, pero SOLO en los proveedores
    # cuyos nombres son alias estables que el listado no siempre enumera
    # (openai, mistral…). En Ollama (nube y local) el listado es exhaustivo:
    # lo que no está, está retirado o borrado, y protegerlo era lo que dejaba
    # deepseek-v4-flash y qwen3-coder:480b en el config meses después.
    protected = set()
    if provider not in ("ollama_cloud", "ollama"):
        protected = {section.get("default_model")}
        fm = section.get("fast_model") or section.get("fast_models")
        if fm:
            protected.add(fm)
        if extra_protected:
            protected |= set(extra_protected)
    removed = [m for m in removed if m not in protected]

    # Limitar los nuevos al cap (cuántos vigentes se conservan + cuántos caben).
    keep_count = len([m for m in current if m in live_set or m in protected])
    espacio = max(0, CAP_PER_PROVIDER - keep_count)
    added = added_all[:espacio]
    if len(added_all) > len(added):
        log.info("[%s] %d nuevos disponibles; se toman %d (cap %d)",
                 provider, len(added_all), len(added), CAP_PER_PROVIDER)

    if not added and not removed and not duplicados:
        log.info("[%s] sin cambios (%d modelos)", provider, len(current))
        return {"added": [], "removed": []}

    # Clasificar nuevos
    classified = classify_new_models(provider, added, research=research) if added else {}

    if dry_run:
        log.info("[%s] +%d nuevos, -%d retirados, %d duplicados (DRY-RUN)", provider, len(added), len(removed), len(duplicados))
        for a in added:
            log.info("    + %s -> %s", a, classified.get(a, {"specialty": "general"})["specialty"])
        for rm in removed:
            log.info("    - %s", rm)
        return {"added": added, "removed": removed + duplicados}

    # Construir nueva lista: conservar existentes vigentes + agregar nuevos clasificados
    new_seq = CommentedSeq()
    puestos = set()
    for m in section["models"]:
        if isinstance(m, dict) and m.get("name") in removed:
            continue
        if isinstance(m, dict) and m.get("name") in puestos:
            continue  # duplicado exacto
        if isinstance(m, dict):
            puestos.add(m.get("name"))
        new_seq.append(m)
    for name in added:
        cls = classified.get(name)
        if not cls:  # sin clasificación fiable -> general por defecto
            cls = {"specialty": "general", "capabilities": ["instruction_following"], "prompt_types": ["general"]}
        new_seq.append(make_model_map(name, cls))

    # Cap por proveedor
    if len(new_seq) > CAP_PER_PROVIDER:
        log.info("[%s] %d modelos > cap %d; se recorta", provider, len(new_seq), CAP_PER_PROVIDER)
        new_seq = CommentedSeq(list(new_seq)[:CAP_PER_PROVIDER])

    # Reasignar default si quedó retirado
    names_now = {m["name"] for m in new_seq if isinstance(m, dict)}
    if section.get("default_model") not in names_now and new_seq:
        nuevo = new_seq[0]["name"]
        log.warning("[%s] default_model retirado -> reasignado a %s", provider, nuevo)
        section["default_model"] = nuevo

    section["models"] = new_seq
    log.info("[%s] +%d nuevos, -%d retirados -> %d total", provider, len(added), len(removed), len(new_seq))
    return {"added": added, "removed": removed + duplicados}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-research", action="store_true", help="sin web_search")
    ap.add_argument("--only", default="", help="lista de proveedores separados por coma")
    args = ap.parse_args()

    providers = ["openai", "groq", "deepinfra", "together", "anthropic", "deepseek",
                 "z-ai", "mistral", "cohere", "ollama_cloud", "openrouter", "ollama"]
    if args.only:
        providers = [p.strip() for p in args.only.split(",") if p.strip()]

    log.info("=== Sync de modelos %s (dry_run=%s, research=%s) ===",
             datetime.datetime.now().isoformat(timespec="seconds"), args.dry_run, not args.no_research)

    import agents
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.allow_duplicate_keys = True  # tolera duplicados (toma el último, como PyYAML)
    with open(CONFIG_PATH) as f:
        cfg = yaml.load(f)

    # AgentManager para reutilizar los clientes ya autenticados
    plain_cfg = dict(cfg)
    mgr = agents.AgentManager(plain_cfg, verify=False)

    critical = collect_critical_models(cfg)
    log.info("Referencias del flujo (evaluation_models/moa/task_models/…): %d",
             sum(len(v) for v in critical.values()))
    desactivados = set(cfg.get("disabled_providers", []) or [])

    summary = {}
    vigentes = {}
    for p in providers:
        if p in desactivados:
            log.info("[%s] desactivado (disabled_providers) -> se omite", p)
            continue
        live = list_provider_models(mgr, p)
        if live is None:
            log.info("[%s] listado no disponible -> se omite", p)
            continue
        vigentes[p] = [m for m in live if is_chat_model(m)]
        res = sync_provider(cfg, p, live, research=not args.no_research,
                            dry_run=args.dry_run, extra_protected=critical.get(p, set()))
        if res:
            summary[p] = res

    reparados = reparar_referencias(cfg, vigentes, dry_run=args.dry_run)
    changed = bool(reparados) or any(s["added"] or s["removed"] for s in summary.values())
    if args.dry_run:
        log.info("=== DRY-RUN terminado. Proveedores con cambios: %s · referencias a reparar: %d ===",
                 [p for p, s in summary.items() if s["added"] or s["removed"]], len(reparados))
        return
    if not changed:
        log.info("=== Sin cambios; config.yaml no se modifica ===")
        return

    # Backup + escribir
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, f"config.yaml.bak.sync-{ts}")
    shutil.copy2(CONFIG_PATH, backup)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f)
    log.info("=== config.yaml actualizado. Backup: %s ===", backup)
    log.info("Resumen: %s", {p: {"+": len(s["added"]), "-": len(s["removed"])} for p, s in summary.items()})


if __name__ == "__main__":
    main()
