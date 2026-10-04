#!/usr/bin/env python3
"""Benchmark por tarea: ¿qué modelo de Ollama Cloud (y de DeepSeek) responde
mejor cada tipo de tarea de MALLO? Mide latencia + calidad (LLM-judge) y
propone, por tarea: cascada de Ollama Cloud (top-3) + mejor fallback DeepSeek.

Uso:
  python3 tools/benchmark_tasks.py            # corrida completa
  python3 tools/benchmark_tasks.py --quick    # menos candidatos
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
DEEPSEEK = OpenAI(api_key=get_secret("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com/v1")
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

# Candidatos de Ollama Cloud (provider=ollama_cloud) y DeepSeek (provider=deepseek)
CANDIDATES = [
    ("ollama_cloud", "gpt-oss:120b"),
    ("ollama_cloud", "gpt-oss:20b"),
    ("ollama_cloud", "deepseek-v3.2"),
    ("ollama_cloud", "glm-5.2"),
    ("ollama_cloud", "qwen3-coder:480b"),
    ("ollama_cloud", "mistral-large-3:675b"),
    ("ollama_cloud", "minimax-m3"),
    ("ollama_cloud", "gemma4:31b"),
    ("deepseek", "deepseek-v4-flash"),
    ("deepseek", "deepseek-v4-pro"),
]
QUICK = [
    ("ollama_cloud", "gpt-oss:120b"),
    ("ollama_cloud", "deepseek-v3.2"),
    ("ollama_cloud", "qwen3-coder:480b"),
    ("deepseek", "deepseek-v4-flash"),
]


def generate(provider, model, prompt):
    client = OLLAMA if provider == "ollama_cloud" else DEEPSEEK
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
    cands = QUICK if args.quick else CANDIDATES

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
        ds = [r for r in rows if r["provider"] == "deepseek"]
        cascada = [r["model"] for r in oc[:3]]
        fallback = ds[0]["model"] if ds else None
        summary[tid] = {"cascada_ollama_cloud": cascada,
                        "fallback_deepseek": fallback,
                        "ranking": rows}
        print(f"\n[{tid}]")
        print(f"  cascada (Ollama Cloud): {cascada}")
        print(f"  fallback (DeepSeek):    {fallback}")

    with open(os.path.join(ROOT, "tools", "benchmark_results.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n=== Guardado: tools/benchmark_results.json ===")

    if args.apply:
        apply_to_config(summary)


def apply_to_config(summary):
    """Escribe las cascadas del benchmark en config.yaml (evaluation_models + moa).

    Mapeo etapa→tarea: initial←classify, final←general, meta_analysis←summary.
    Preserva formato con ruamel y hace backup. Solo toca esas dos secciones.
    """
    import datetime
    import shutil
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedSeq

    def casc(task):
        return summary.get(task, {}).get("cascada_ollama_cloud", []) or ["gpt-oss:120b"]

    def ds(task):
        return summary.get(task, {}).get("fallback_deepseek") or "deepseek-v4-flash"

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.allow_duplicate_keys = True
    cfg_path = os.path.join(ROOT, "config.yaml")
    cfg = yaml.load(open(cfg_path))

    em = cfg["evaluation_models"]
    for stage, task in (("initial", "classify"), ("final", "general"), ("meta_analysis", "summary")):
        c = casc(task)
        em[stage]["api"] = "ollama_cloud"
        em[stage]["model"] = c[0]
        em[stage]["backup_api"] = "deepseek"
        em[stage]["backup_model"] = ds(task)
        em[stage]["backup_api2"] = "ollama_cloud"
        em[stage]["backup_model2"] = c[1] if len(c) > 1 else c[0]

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
    shutil.copy2(cfg_path, f"{cfg_path}.bak.bench-{ts}")
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    print(f"=== config.yaml actualizado con las cascadas (backup .bak.bench-{ts}) ===")
    print(f"  evaluation_models.final/meta = {agg} | initial = {casc('classify')[0]}")


if __name__ == "__main__":
    main()
