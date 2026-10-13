#!/usr/bin/env python3
"""Benchmark por tarea: ¿qué modelo VIGENTE de Ollama Cloud responde mejor cada
tipo de tarea de MALLO? Mide latencia + calidad (LLM-judge) y propone, por
tarea, una cascada de Ollama Cloud (top-3).

Desde 2026-10-04 es un INFORME: el timer mensual lo corre sin --apply. Las
cascadas de config.yaml se eligieron midiendo con los prompts reales de cada
flujo (docs/MODELOS-OLLAMA-CLOUD-POR-FLUJO-2026-10-04.md); seis tareas de
juguete juzgadas por un LLM no bastan para cambiarlas solas. Antes, --apply
instaló qwen3-coder:480b (luego retirado) y DeepSeek como respaldo de todas las
etapas con su clave ya inválida.

Los candidatos salen del catálogo vigente (https://ollama.com/api/tags), así
que un modelo retirado deja de medirse sin tocar este fichero.

Uso:
  python3 tools/benchmark_tasks.py            # corrida completa (informe)
  python3 tools/benchmark_tasks.py --quick    # menos candidatos
  python3 tools/benchmark_tasks.py --apply    # además escribe evaluation_models/moa
Salida: tools/benchmark_results.json
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from load_secrets import load_secrets, get_secret  # noqa: E402
load_secrets()
from openai import OpenAI  # noqa: E402

OLLAMA = OpenAI(api_key=get_secret("OLLAMA_CLOUD_API_KEY"), base_url="https://ollama.com/v1")
JUDGE_MODEL = "gpt-oss:120b"   # juez (Ollama Cloud)

# Tareas DISCRIMINANTES del flujo de MALLO (difíciles, con respuesta verificable,
# para que la CALIDAD separe a los modelos, no solo la latencia).
TASKS = [
    {"id": "general", "prompt": "Explica en 4-5 frases por qué el cielo es azul, mencionando la dispersión de Rayleigh y por qué los atardeceres son rojizos. Debe ser preciso y autocontenido.",
     "criterio": "exactitud física (Rayleigh, dependencia de longitud de onda, por qué rojo al atardecer), claridad y completitud"},
    {"id": "coding", "prompt": "Escribe una función Python `merge_intervalos(intervalos)` que reciba una lista de [inicio, fin] y devuelva los intervalos fusionados ordenados. Maneja lista vacía, intervalos solapados y adyacentes. Incluye complejidad. Solo código + breve comentario de complejidad.",
     "criterio": "corrección del algoritmo (ordenar + fusionar), manejo de bordes (vacío, solapados, adyacentes), eficiencia O(n log n) correcta, código limpio"},
    {"id": "reasoning", "prompt": "Un bate y una pelota cuestan $1.10 en total. El bate cuesta $1.00 más que la pelota. ¿Cuánto cuesta la pelota? Razona paso a paso y da el valor exacto.",
     "criterio": "respuesta correcta = $0.05 (NO $0.10), con razonamiento algebraico válido; penaliza fuerte la respuesta intuitiva errónea"},
    {"id": "math", "prompt": "Resuelve la ecuación diferencial dy/dx = y con y(0)=2, y luego evalúa y(1). Muestra el procedimiento y el valor numérico final.",
     "criterio": "solución correcta y=2e^x, y(1)=2e≈5.4366; procedimiento válido y valor numérico correcto"},
    {"id": "summary", "prompt": "Resume en EXACTAMENTE 2 frases, sin inventar datos: 'La acción de tutela (art. 86 CP de Colombia) protege derechos fundamentales; procede cuando no hay otro medio de defensa judicial o para evitar un perjuicio irremediable. El juez debe resolverla en 10 días y la decisión puede impugnarse ante el superior, que decide en 20 días. La Corte Constitucional puede revisarla eventualmente.'",
     "criterio": "exactamente 2 frases, fidelidad total (10 días, 20 días, impugnación, revisión eventual), sin alucinar ni omitir lo esencial"},
    {"id": "classify", "prompt": "Clasifica en UNA sola categoría {coding, legal, math, general} y responde SOLO con la palabra: 'Necesito calcular el interés compuesto de una inversión a 5 años'.",
     "criterio": "clasificación correcta = math (cálculo financiero), respuesta de UNA palabra sin texto extra"},
]

# Candidatos: el catálogo vigente de Ollama Cloud, menos los que no son de chat
# general. (DeepSeek directo salió el 2026-10-04: su clave da 401.)
NO_CANDIDATOS = {"nemotron-3-nano:30b"}  # 30B de razonamiento corto, no compite aquí


def catalogo_vigente():
    import httpx
    r = httpx.get("https://ollama.com/api/tags", timeout=30)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def candidatos(quick=False):
    vig = [m for m in catalogo_vigente() if m not in NO_CANDIDATOS]
    if quick:
        prefer = ["gpt-oss:120b", "glm-5.3", "kimi-k3", "gemma4:31b"]
        vig = [m for m in prefer if m in vig]
    return [("ollama_cloud", m) for m in vig]


def generate(provider, model, prompt):
    client = OLLAMA
    t = time.time()
    try:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=4000,
        )
        msg = r.choices[0].message
        content = (msg.content or "").strip() or (getattr(msg, "reasoning_content", None) or "").strip()
        return content, round(time.time() - t, 2), None
    except Exception as e:  # noqa: BLE001
        return "", round(time.time() - t, 2), str(e)[:80]


def judge(task, answer):
    prompt = (
        f"Eres un evaluador EXIGENTE. Califica de 0 a 100 esta respuesta a una tarea '{task['id']}'.\n"
        f"RÚBRICA (criterio): {task['criterio']}.\n\n"
        "Penaliza con dureza: errores factuales o de cálculo (−40 o más), incumplir el formato "
        "pedido (−20), omisiones importantes (−20), alucinaciones (−40), verbosidad innecesaria (−10). "
        "Solo respuestas perfectas, correctas y completas merecen 90-100.\n\n"
        f"TAREA: {task['prompt']}\n\nRESPUESTA A EVALUAR:\n{answer[:2000]}\n\n"
        "Devuelve SOLO un entero de 0 a 100. Solo el número."
    )
    try:
        r = OLLAMA.chat.completions.create(
            model=JUDGE_MODEL, messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=2500,
        )
        msg = r.choices[0].message
        txt = (msg.content or "").strip() or (getattr(msg, "reasoning_content", None) or "")
        import re
        nums = re.findall(r"\b(100|[0-9]{1,2})\b", txt)
        return int(nums[-1]) if nums else 0   # el último número suele ser el veredicto
    except Exception:  # noqa: BLE001
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="escribe las cascadas en config.yaml (evaluation_models + moa)")
    args = ap.parse_args()
    cands = candidatos(args.quick)

    results = {t["id"]: [] for t in TASKS}
    for task in TASKS:
        print(f"\n### TAREA: {task['id']}")
        for provider, model in cands:
            ans, lat, err = generate(provider, model, task["prompt"])
            if err or not ans:
                print(f"  {model:28} FALLO ({err or 'vacío'})")
                continue
            score = judge(task, ans)
            # Calidad (0-100) domina; latencia penaliza suave (máx ~6 pts a 20s).
            combined = round(score - 0.3 * lat, 1)
            results[task["id"]].append(
                {"provider": provider, "model": model, "score": score,
                 "latency": lat, "combined": combined}
            )
            print(f"  {model:28} calidad={score:3}  {lat:5.1f}s  combinado={combined:5.1f}")

    # Ranking + cascadas sugeridas (por score combinado: calidad sobre latencia)
    print("\n\n========== CASCADAS SUGERIDAS POR TAREA (calidad>latencia) ==========")
    summary = {}
    for tid, rows in results.items():
        rows.sort(key=lambda r: (-r["combined"], r["latency"]))
        oc = [r for r in rows if r["provider"] == "ollama_cloud"]
        cascada = [r["model"] for r in oc[:3]]
        summary[tid] = {"cascada_ollama_cloud": cascada,
                        "ranking": rows}
        print(f"\n[{tid}]")
        print(f"  cascada (Ollama Cloud): {cascada}")

    with open(os.path.join(ROOT, "tools", "benchmark_results.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n=== Guardado: tools/benchmark_results.json ===")

    if args.apply:
        apply_to_config(summary)


def _familia(m):
    return m.split(":")[0].split("-")[0]


def apply_to_config(summary):
    """Escribe las cascadas del benchmark en config.yaml (evaluation_models + moa).

    Mapeo etapa→tarea: initial←classify, final←general, meta_analysis←summary.
    Respaldo 1: el mejor de OTRA familia en Ollama Cloud; respaldo 2: Mistral
    (otro proveedor). Copia previa en /root/backups/MALLO/config-bak/.
    """
    import datetime
    import shutil
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedSeq

    def casc(task):
        return summary.get(task, {}).get("cascada_ollama_cloud", []) or ["gpt-oss:120b"]

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.allow_duplicate_keys = True
    cfg_path = os.path.join(ROOT, "config.yaml")
    cfg = yaml.load(open(cfg_path))
    mistral_default = cfg.get("mistral", {}).get("default_model", "mistral-small-latest")

    em = cfg["evaluation_models"]
    for stage, task in (("initial", "classify"), ("final", "general"), ("meta_analysis", "summary")):
        c = casc(task)
        otra = next((m for m in c[1:] if _familia(m) != _familia(c[0])), c[1] if len(c) > 1 else c[0])
        em[stage]["api"] = "ollama_cloud"
        em[stage]["model"] = c[0]
        em[stage]["backup_api"] = "ollama_cloud"
        em[stage]["backup_model"] = otra
        em[stage]["backup_api2"] = "mistral"
        em[stage]["backup_model2"] = mistral_default

    agg = casc("general")[0]
    cfg["moa"]["api"] = "ollama_cloud"
    cfg["moa"]["aggregator_model"] = agg
    pool = []
    for t in ("classify", "coding", "reasoning", "summary"):
        for m in casc(t):
            if m != agg and m not in pool:
                pool.append(m)
    cfg["moa"]["proposal_models"] = CommentedSeq(pool[:4] or [agg])

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_dir = "/root/backups/MALLO/config-bak"
    os.makedirs(bak_dir, exist_ok=True)
    shutil.copy2(cfg_path, os.path.join(bak_dir, f"config.yaml.bak.bench-{ts}"))
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    print(f"=== config.yaml actualizado con las cascadas (backup {bak_dir}/config.yaml.bak.bench-{ts}) ===")
    print(f"  evaluation_models.final/meta = {agg} | initial = {casc('classify')[0]}")


if __name__ == "__main__":
    main()
