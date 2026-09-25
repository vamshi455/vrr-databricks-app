"""PDF → pgvector knowledge ingestion (OSS equivalent of 11_knowledge_ingest).

Flow (see docs/knowledge-flow.md):
  1. REGISTER   a document dropped in ./knowledge_uploads/ → knowledge_registry
                (pending_review). PDF/txt/md/html/docx/csv — see document_loaders.py
  2. REVIEW     a HUMAN sets status='approved' (VRR-relevant only) — not automated
  3. INGEST     load (document_loaders) → chunk (text_splitters) → PII detect/REDACT
                (core.knowledge.redact_pii) → EMBED (local model) → INSERT into
                vrr_agent.reservoir_knowledge (pgvector), PII never reaching the DB
  4. SEARCH     the agent's SEARCH_KNOWLEDGE tool runs `embedding <=> query` (cosine)

Embeddings are produced by a LOCAL model (Ollama `nomic-embed-text`, 768-dim) so the
whole path stays offline + free. Swap `embed()` for any encoder that matches the
vector(768) column in schema.sql.
"""
from __future__ import annotations

import hashlib
import os
import pathlib

import httpx
import psycopg
from pgvector.psycopg import register_vector

from ..config import load_config
from ..core import knowledge as kn

CFG = load_config()
UPLOAD_DIR = os.environ.get("VRR_KNOWLEDGE_DIR", "./knowledge_uploads")
EMBED_MODEL = os.environ.get("VRR_EMBED_MODEL", "nomic-embed-text")   # 768-dim, local


def _conn():
    c = psycopg.connect(CFG.pg_dsn)
    register_vector(c)                       # enables the `vector` type for pgvector
    return c


def embed(text: str) -> list[float]:
    """One embedding, `CFG.embed_dim` wide.

    `VRR_EMBED_PROVIDER=ollama` (default): a local model, offline.
    `VRR_EMBED_PROVIDER=databricks`: a Model Serving embedding endpoint (e.g.
    `databricks-gte-large-en`, 1024-dim) through its OpenAI-compatible API. Either way
    the width must match the `vector(N)` column — the bootstrap writes N from the same
    config value, and a mismatch raises here rather than storing a truncated vector.
    """
    if CFG.embed_provider == "databricks":
        from openai import OpenAI

        from .. import databricks_env as DBX

        client = OpenAI(base_url=DBX.serving_base_url(), api_key=DBX.bearer_token(),
                        timeout=60)
        vec = client.embeddings.create(model=EMBED_MODEL, input=text).data[0].embedding
    else:
        r = httpx.post(f"{CFG.llm_base_url}/api/embeddings",
                       json={"model": EMBED_MODEL, "prompt": text}, timeout=60)
        vec = r.json()["embedding"]
    if len(vec) != CFG.embed_dim:
        raise RuntimeError(f"embedder returned {len(vec)} dims but VRR_EMBED_DIM is "
                           f"{CFG.embed_dim}; fix the env, then re-run the bootstrap")
    return vec


def normalize_page(text: str) -> str:
    """Undo PDF layout artefacts before chunking.

    Extracted PDF text carries hard line wraps and run of spaces from the page layout;
    left alone they leak into the chunks (and into anything quoting them). Collapse the
    intra-paragraph wrapping while keeping blank lines, which `core.knowledge.chunk_text`
    uses as paragraph boundaries.
    """
    import re
    text = (text or "").replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)          # unwrap single newlines
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def register_new() -> int:
    """Step 1 — register new documents in the volume as pending_review."""
    from . import document_loaders as DL

    n = 0
    with _conn() as c, c.cursor() as cur:
        for f in os.listdir(UPLOAD_DIR):
            if pathlib.Path(f).suffix.lower() not in DL.SUFFIX_LOADERS:
                continue          # .pdf/.txt/.md/.html/.docx/.csv — see document_loaders
            doc_id = hashlib.sha1(f.encode()).hexdigest()
            cur.execute(
                "INSERT INTO vrr_agent.knowledge_registry (doc_id, file_name, status) "
                "VALUES (%s, %s, 'pending_review') ON CONFLICT (doc_id) DO NOTHING",
                (doc_id, f))
            n += cur.rowcount
        c.commit()
    return n


def _embed_one(cur, doc_id: str, fname: str, strategy: str,
               doc_kind: str = "reservoir") -> dict:
    """Load → chunk → redact → embed → INSERT one registered document.

    Factored out of `ingest_approved` so the API can embed a SINGLE document the instant
    a reviewer approves it (`api/routes_knowledge.py`), instead of the browser waiting on
    a sweep of every approved-but-not-ingested row. Same code either way — an upload and
    a folder drop must not be able to produce different chunks from the same bytes.

    Takes a live cursor rather than opening its own connection, so approving and embedding
    commit together: a row marked approved whose chunks failed to write is a document the
    reviewer believes is searchable and is not.
    """
    from . import document_loaders as DL
    from . import text_splitters as TS

    pages = DL.load_file(os.path.join(UPLOAD_DIR, fname))
    for p in pages:                           # undo PDF layout wrapping before split
        p.page_content = normalize_page(p.page_content)
    kinds, seq = set(), 0
    for piece in TS.split_documents(pages, strategy=strategy):
        clean, k = kn.redact_pii(piece.page_content)      # PII never reaches the DB
        kinds.update(k)
        pageno = int(piece.metadata.get("page", 1))
        cur.execute(
            "INSERT INTO vrr_agent.reservoir_knowledge "
            "(chunk_id, doc_id, file_name, page, chunk_seq, text, pii_redacted, "
            " embedding, doc_kind) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (chunk_id) DO UPDATE "
            "SET text=EXCLUDED.text, embedding=EXCLUDED.embedding, "
            "    doc_kind=EXCLUDED.doc_kind",
            (f"{doc_id}:{pageno}:{seq}", doc_id, fname, pageno, seq,
             clean, bool(k), embed(clean), doc_kind))
        seq += 1
    cur.execute("UPDATE vrr_agent.knowledge_registry SET n_chunks=%s, "
                "pii_found=%s, pii_kinds=%s WHERE doc_id=%s",
                (seq, bool(kinds), ",".join(sorted(kinds)) or None, doc_id))
    return {"doc_id": doc_id, "file_name": fname, "n_chunks": seq,
            "pages": len(pages), "pii_kinds": sorted(kinds), "doc_kind": doc_kind}


def ingest_document(doc_id: str, file_name: str, strategy: str | None = None,
                    doc_kind: str = "reservoir") -> dict:
    """Embed exactly one document, now. The approval endpoint's whole back half."""
    strategy = strategy or os.environ.get("VRR_CHUNK_STRATEGY", "recursive")
    with _conn() as c, c.cursor() as cur:
        out = _embed_one(cur, doc_id, file_name, strategy, doc_kind)
        c.commit()
    return out


def delete_document(doc_id: str) -> int:
    """Drop a document's chunks out of the vector index. Returns rows removed.

    The registry row is kept by the caller: what was ingested and by whom is a record,
    and deleting the evidence along with the data is the failure mode `chat_history`
    already avoids by making "clear" a per-user cutoff.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM vrr_agent.reservoir_knowledge WHERE doc_id=%s", (doc_id,))
        n = cur.rowcount
        cur.execute("UPDATE vrr_agent.knowledge_registry SET n_chunks=NULL WHERE doc_id=%s",
                    (doc_id,))
        c.commit()
    return n


def preview_document(file_name: str, strategy: str | None = None,
                     max_chars: int = 1200) -> dict:
    """What a reviewer sees BEFORE approving: real extracted text, and the PII in it.

    Runs the actual loader and splitter — not a guess — because the question a reviewer
    is answering is "would embedding this be useful and safe", and a PDF that extracts to
    nothing (a scan with no OCR layer) looks identical to a good one until you parse it.
    Embeds NOTHING; no model is called and no row is written.
    """
    from . import document_loaders as DL
    from . import text_splitters as TS

    strategy = strategy or os.environ.get("VRR_CHUNK_STRATEGY", "recursive")
    pages = DL.load_file(os.path.join(UPLOAD_DIR, file_name))
    for p in pages:
        p.page_content = normalize_page(p.page_content)
    pieces = TS.split_documents(pages, strategy=strategy)
    full = "\n\n".join(p.page_content for p in pages)
    hits = kn.detect_pii(full)
    counts: dict[str, int] = {}
    for h in hits:
        counts[h["kind"]] = counts.get(h["kind"], 0) + 1
    # The preview is redacted too. A reviewer does not need to READ the credential to
    # decide the document contains one, and an unredacted preview would put PII in a
    # browser and an access log after this pipeline went to the trouble of keeping it
    # out of the database.
    redacted, _ = kn.redact_pii(full[:max_chars])
    return {
        "file_name": file_name, "pages": len(pages), "n_chunks": len(pieces),
        "total_chars": len(full), "strategy": strategy,
        "extracted_text": redacted,
        "truncated": len(full) > max_chars,
        "pii_kinds": counts,
        # A PDF whose pages carry no text layer produces chunks of nothing, which embed
        # into noise and pollute every top-k. Say so rather than let it through quietly.
        "empty_extraction": len(full.strip()) < 200,
    }


def ingest_approved(strategy: str | None = None) -> int:
    """Step 3 — load + chunk + PII-redact + embed every approved-but-not-ingested doc.

    Loading goes through `document_loaders` (so .txt/.html/.docx/.csv ingest exactly like
    a PDF does) and chunking through `text_splitters` (default `recursive`, the strategy
    that measured best — see `make chunks`). `VRR_CHUNK_STRATEGY` overrides it; changing
    it means re-ingesting, since chunk boundaries are baked into the stored embeddings.
    """
    strategy = strategy or os.environ.get("VRR_CHUNK_STRATEGY", "recursive")
    done = 0
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT doc_id, file_name, coalesce(doc_kind, 'reservoir') "
                    "FROM vrr_agent.knowledge_registry "
                    "WHERE status='approved' AND n_chunks IS NULL")
        for doc_id, fname, kind in cur.fetchall():
            _embed_one(cur, doc_id, fname, strategy, kind)
            done += 1
        c.commit()
    return done


def search(query: str, k: int = 5, min_score: float | None = None,
           doc_kind: str | None = "reservoir") -> list[dict]:
    """Step 4 — the SEARCH_KNOWLEDGE tool: cosine nearest chunks (pgvector `<=>`).

    Filtered by a similarity FLOOR, not just top-k. Without one, `LIMIT k` always
    returns k rows: ask about something the corpus never covered and the model still
    receives four confident-looking excerpts, which is precisely the situation where it
    invents an answer instead of saying it does not know. Below the floor we return
    nothing, and the caller says so.

    `VRR_RETRIEVAL_MIN_SCORE` tunes it (default 0.35); pass `min_score=0` to see the
    raw ranking, which is what `retrieval_check` wants when comparing chunkers.

    `doc_kind` picks the CORPUS, and defaults to `reservoir` so every existing caller
    keeps searching exactly what it always searched. The two corpora share a table but
    are never searched together: top-k is a fixed budget, so letting a user-guide page
    about the Approvals screen compete with the injection-change procedure means one of
    them loses a slot it needed. `doc_kind=None` searches everything, which is what
    `retrieval_check` wants when it is measuring the chunker rather than answering.
    """
    floor = CFG.retrieval_min_score if min_score is None else min_score
    vec = str(embed(query))              # embed ONCE, reuse for score + ordering
    with _conn() as c:
        with c.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                "SELECT file_name, page, text, doc_kind, "
                "       1 - (embedding <=> %(v)s::vector) AS score "
                "FROM vrr_agent.reservoir_knowledge "
                "WHERE 1 - (embedding <=> %(v)s::vector) >= %(floor)s "
                "  AND (%(kind)s::text IS NULL OR doc_kind = %(kind)s::text) "
                "ORDER BY embedding <=> %(v)s::vector LIMIT %(k)s",
                {"v": vec, "k": k, "floor": floor, "kind": doc_kind})
            return cur.fetchall()


if __name__ == "__main__":
    print("registered:", register_new(), "| ingested approved:", ingest_approved())
