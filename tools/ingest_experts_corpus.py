#!/usr/bin/env python3
"""Ingiere los system prompts de los 26 expertos (assistants_export.json) al
corpus jurídico del Mentor (claude-for-legal-colombia), clasificados por área
(assistants_map.yaml). Da contenido a las áreas nuevas (judicial-admin,
ia-gobernanza, etc.) y refuerza las jurídicas con el conocimiento del experto.

Idempotente (reemplaza por doc_id). Hace backup de la BD antes de escribir.
Uso: python3 tools/ingest_experts_corpus.py
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("LEGAL_CORPUS_DB", "/root/claude-for-legal-colombia/corpus.sqlite3")
EXPORT = os.path.join(ROOT, "tools", "assistants_export.json")
MAP = os.path.join(ROOT, "tools", "assistants_map.yaml")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def chunk_text(t, size=800):
    words = t.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)] or [t]


def main():
    export = json.load(open(EXPORT))
    amap = (yaml.safe_load(open(MAP)) or {}).get("asistentes", {})

    shutil.copy2(DB, f"{DB}.bak.experts-{datetime.datetime.now():%Y%m%d_%H%M%S}")
    c = sqlite3.connect(DB)
    now = datetime.datetime.now().isoformat()
    ndocs = nchunks = 0
    for rec in export:
        aid = rec["id"]
        instr = (rec.get("instructions") or "").strip()
        if not instr:
            continue
        area = (amap.get(aid) or {}).get("area", "general")
        name = rec.get("config_name") or rec.get("name") or aid
        doc_id = f"experto-{slug(name)}"
        c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM chunks_fts WHERE doc_id=?", (doc_id,))
        chunks = chunk_text(instr)
        c.execute(
            "INSERT INTO documents (doc_id,sha1,categoria,area,titulo,fuente,ext,n_chunks,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, hashlib.sha1(instr.encode()).hexdigest(), "agente", area, name,
             "agentes-mallo", ".md", len(chunks), now),
        )
        for i, ch in enumerate(chunks):
            c.execute(
                "INSERT INTO chunks_fts (chunk_id,doc_id,categoria,area,titulo,text)"
                " VALUES (?,?,?,?,?,?)",
                (f"{doc_id}-{i}", doc_id, "agente", area, name, ch),
            )
            nchunks += 1
        ndocs += 1
    c.commit()
    print(f"Ingeridos {ndocs} documentos de expertos, {nchunks} chunks.")
    print("Distribución por área (fuente=agentes-mallo):")
    for a, n in c.execute(
        "SELECT area, COUNT(*) FROM documents WHERE fuente='agentes-mallo' GROUP BY area ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {a}: {n}")
    c.close()


if __name__ == "__main__":
    main()
