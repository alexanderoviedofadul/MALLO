#!/usr/bin/env python3
"""Batería E2E de la API de MALLO: simula los casos de uso y reporta PASS/FAIL.
Uso: python3 tools/e2e_smoke.py
"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:9006"
# Las claves se leen de .streamlit/secrets.toml (nunca literales en el repo).
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from load_secrets import load_secrets, get_secret  # noqa: E402
load_secrets()
MALLO_KEY = (get_secret("MALLO_API_KEYS") or "").split(",")[0].strip()
C360_KEY = get_secret("CEDULA360_API_KEY") or ""
H_MALLO = {"X-API-Key": MALLO_KEY, "Content-Type": "application/json"}
H_C360 = {"X-API-Key": C360_KEY, "Content-Type": "application/json"}

ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    mark = "✅" if cond else "❌"
    if cond:
        ok += 1
    else:
        fail += 1
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))


def post(path, headers, payload, timeout=180):
    return httpx.post(f"{BASE}{path}", headers=headers, json=payload, timeout=timeout)


def get(path, headers=None, timeout=60):
    return httpx.get(f"{BASE}{path}", headers=headers or {}, timeout=timeout)


print("=== 1. Salud y autenticación ===")
r = get("/health")
check("/health público 200", r.status_code == 200)
r = get("/v1/models")
check("/v1/models sin auth → 401", r.status_code == 401)
r = get("/v1/models", H_MALLO)
provs = r.json().get("providers", {}) if r.status_code == 200 else {}
check("/v1/models con key MALLO", r.status_code == 200, f"{len(provs)} proveedores")
r = get("/v1/models", H_C360)
check("/v1/models con key cedula360", r.status_code == 200)

print("\n=== 2. Chat por proveedor activo ===")
import yaml  # noqa: E402
_cfg = yaml.safe_load(open(_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "config.yaml")))
for prov in ("ollama_cloud", "mistral", "minimax"):
    model = _cfg[prov]["default_model"]
    r = post("/v1/chat", H_MALLO, {"query": "di OK en una palabra", "provider": prov, "model": model}, 90)
    resp = r.json().get("response", "") if r.status_code == 200 else r.text[:60]
    check(f"chat {prov}:{model}", r.status_code == 200 and bool(resp), resp[:40])
# Control negativo: un proveedor de disabled_providers no se inicializa → 400.
for prov in _cfg.get("disabled_providers", [])[:2]:
    r = post("/v1/chat", H_MALLO, {"query": "di OK", "provider": prov, "model": _cfg[prov]["default_model"]}, 30)
    check(f"{prov} desactivado → 400", r.status_code == 400, r.text[:60])

print("\n=== 3. Streaming SSE ===")
deltas = 0
try:
    with httpx.stream("POST", f"{BASE}/v1/chat/stream", headers=H_MALLO,
                      json={"query": "cuenta 1 2 3", "provider": "ollama_cloud", "model": "gpt-oss:20b"},
                      timeout=90) as s:
        for line in s.iter_lines():
            if line.startswith("data:") and "delta" in line:
                deltas += 1
            if "[DONE]" in line:
                break
    check("streaming SSE emite deltas", deltas > 0, f"{deltas} eventos")
except Exception as e:
    check("streaming SSE emite deltas", False, str(e)[:50])

print("\n=== 4. Orquestación dinámica (enrutamiento) ===")
r = post("/v1/orchestrate", H_MALLO, {"query": "¿Qué es una estrella?"}, 90)
ag = r.json().get("selected_agent", {}) if r.status_code == 200 else {}
check("general → ollama_cloud", ag.get("agent") == "ollama_cloud", str(ag))
r = post("/v1/orchestrate", H_MALLO, {"query": "Escribe quicksort en Python"}, 90)
ag = r.json().get("selected_agent", {}) if r.status_code == 200 else {}
check("coding → ollama_cloud", ag.get("agent") == "ollama_cloud", str(ag))
r = post("/v1/orchestrate", H_MALLO,
         {"query": "¿Cómo presento una acción de tutela por violación al debido proceso?"}, 180)
ag = r.json().get("selected_agent", {}) if r.status_code == 200 else {}
check("jurídico → assistant (experto /api/legal)", ag.get("agent") == "assistant", str(ag))

print("\n=== 5. Integración cedula360 ===")
r = get("/v1/cedula360/gating", H_MALLO)
check("/v1/cedula360/gating", r.status_code == 200 and "ranking_planes" in r.json())
r = get("/v1/cedula360/consulta/901434933?tipo=NIT", H_MALLO, timeout=120)
res = r.json().get("resultado", {}) if r.status_code == 200 else {}
check("/v1/cedula360/consulta NIT", r.status_code == 200 and bool(res), f"claves: {list(res)[:4]}")
r = post("/v1/orchestrate", H_MALLO,
         {"query": "Resume esta empresa", "enrich_cedula360": {"documento": "901434933", "tipo": "NIT"}}, 150)
check("orchestrate + enrich cedula360", r.status_code == 200 and r.json().get("cedula360_enriched") is True)

print("\n=== 6. Auth con key de cedula360 (gating) ===")
r = post("/v1/chat", H_C360, {"query": "di hola", "provider": "ollama_cloud", "model": "gemma4:31b"}, 60)
check("chat autenticado con key c360", r.status_code == 200, r.json().get("principal", "") if r.status_code == 200 else "")

print(f"\n========== RESULTADO: {ok} PASS / {fail} FAIL ==========")
sys.exit(1 if fail else 0)
