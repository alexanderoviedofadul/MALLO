#!/usr/bin/env python3
"""Extrae los OpenAI Assistants (asst_*) declarados en config.yaml.

Para cada asistente recupera: name, instructions (system prompt), model,
descripción, herramientas, y los archivos adjuntos (code_interpreter file_ids
y/o vector stores de file_search). Descarga los archivos a docs/assistants_export/.

REQUIERE OPENAI_API_KEY temporalmente activa en secrets.toml. Tras correrlo,
vuelve a comentar la key.

Uso:
  python3 tools/extract_assistants.py
Salida:
  tools/assistants_export.json
  docs/assistants_export/<asst_id>/<archivos>
"""

import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from load_secrets import load_secrets, get_secret  # noqa: E402
load_secrets()
from openai import OpenAI  # noqa: E402

CONFIG = os.path.join(ROOT, "config.yaml")
OUT_JSON = os.path.join(ROOT, "tools", "assistants_export.json")
OUT_DIR = os.path.join(ROOT, "docs", "assistants_export")


def list_assistant_files(client, asst):
    """Devuelve la lista de file_ids del asistente (code_interpreter + vector stores)."""
    file_ids = set()
    tr = getattr(asst, "tool_resources", None)
    if tr:
        ci = getattr(tr, "code_interpreter", None)
        if ci and getattr(ci, "file_ids", None):
            file_ids.update(ci.file_ids)
        fs = getattr(tr, "file_search", None)
        if fs and getattr(fs, "vector_store_ids", None):
            for vs_id in fs.vector_store_ids:
                try:
                    vfiles = client.vector_stores.files.list(vector_store_id=vs_id)
                    for vf in vfiles.data:
                        file_ids.add(vf.id)
                except Exception as e:  # noqa: BLE001
                    print(f"    (no se pudo listar vector store {vs_id}: {e})")
    # Compatibilidad con API v1 (file_ids directo)
    if getattr(asst, "file_ids", None):
        file_ids.update(asst.file_ids)
    return sorted(file_ids)


def main():
    key = get_secret("OPENAI_API_KEY")
    if not key:
        print("ERROR: OPENAI_API_KEY no está activa. Descoméntala temporalmente en "
              ".streamlit/secrets.toml y reintenta.")
        sys.exit(1)
    client = OpenAI(api_key=key)

    cfg = yaml.safe_load(open(CONFIG))
    assistants = cfg.get("specialized_assistants", [])
    os.makedirs(OUT_DIR, exist_ok=True)

    export = []
    for a in assistants:
        aid, name = a.get("id"), a.get("name")
        print(f"→ {name} ({aid})")
        rec = {"id": aid, "config_name": name, "keywords": a.get("keywords", [])}
        try:
            asst = client.beta.assistants.retrieve(aid)
            rec.update({
                "name": asst.name,
                "model": asst.model,
                "description": getattr(asst, "description", None),
                "instructions": asst.instructions or "",
                "tools": [getattr(t, "type", str(t)) for t in (asst.tools or [])],
            })
            file_ids = list_assistant_files(client, asst)
            rec["file_ids"] = file_ids
            rec["files"] = []
            adir = os.path.join(OUT_DIR, aid)
            if file_ids:
                os.makedirs(adir, exist_ok=True)
            for fid in file_ids:
                try:
                    meta = client.files.retrieve(fid)
                    fname = meta.filename.replace("/", "_")
                    content = client.files.content(fid).read()
                    with open(os.path.join(adir, fname), "wb") as fh:
                        fh.write(content)
                    rec["files"].append({"id": fid, "filename": meta.filename,
                                         "bytes": getattr(meta, "bytes", len(content))})
                    print(f"    ✓ archivo: {meta.filename}")
                except Exception as e:  # noqa: BLE001
                    print(f"    ✗ archivo {fid}: {e}")
                    rec["files"].append({"id": fid, "error": str(e)})
            print(f"    instructions: {len(rec['instructions'])} chars | archivos: {len(file_ids)}")
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)
            print(f"    ✗ ERROR: {e}")
        export.append(rec)

    with open(OUT_JSON, "w") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    ok = sum(1 for r in export if r.get("instructions"))
    nfiles = sum(len(r.get("files", [])) for r in export)
    print(f"\n=== Extraídos {ok}/{len(export)} con instructions, {nfiles} archivos. "
          f"JSON: {OUT_JSON} ===")
    print("RECORDATORIO: vuelve a comentar OPENAI_API_KEY en secrets.toml.")


if __name__ == "__main__":
    main()
