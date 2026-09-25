"""Chunking — and how to tell a good split from a bad one WITHOUT reading chunks by eye.

Each chunk is embedded in ISOLATION. Whatever context a chunk lost at its boundary is
gone at retrieval time: the embedding cannot see the sentence that finished the thought
on the next chunk. So a split that lands mid-rule doesn't just look untidy, it makes the
rule unfindable.

Measured on the sample below (`make chunks`), fixed-size chunking cuts here:

    "…the response factor rho … It starts │ at 0.85 for a new pattern…"
     └────────── chunk 1 ends ────────────┘└──── chunk 2 begins ──────┘

and the question "what does the response factor rho start at?" then retrieves chunk 1
(which names rho but not the value) — the chunk holding 0.85 ranks 3rd, outside the top-k
that reaches the prompt. Same text, recursive splitting: rank 1.

    strategy    recall@2   MRR
    fixed         0.33     0.56
    recursive     1.00     1.00
    semantic      0.67     0.78   ← over-splits THIS short text; see the note below

Three strategies, cheapest first:

    fixed       CharacterTextSplitter, N chars, boundary-blind      — the baseline to beat
    recursive   RecursiveCharacterTextSplitter, ¶ → line → word     — the default choice
    semantic    embedding-similarity: break where the topic turns   — 1 embed call per
                                                                      sentence, at ingest

Note on semantic chunking: it is not automatically the winner. On short, dense procedure
text like the sample it OVER-SPLITS (9 chunks averaging 83 chars), because consecutive
sentences of a tight rule still read as topic changes to the embedder — and an 83-char
chunk carries too little context to rank well. It earns its cost on long, loosely
structured prose. Raise `threshold` toward 0.9 to split more, lower it to split less, and
let `retrieval_check` decide rather than intuition.

**How to test chunking in short**: `retrieval_check()`. Label a handful of questions with
a phrase that MUST appear in the retrieved chunk, embed each strategy's chunks, and score
recall@k + MRR. That is the whole method — a chunking change is only better if retrieval
gets better, and this is the smallest thing that can say so.

Run: `make chunks`
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from langchain_core.documents import Document

# A VRR procedure excerpt written so that a 200-char fixed split lands mid-rule — the
# same failure real field procedures hit, where the number and what it limits are two
# clauses apart.
SAMPLE = """Injection Change Approval and Response Calibration Procedure

Step limits. Any single injection adjustment must not exceed 15% of the current
rate in a single step. Larger corrections are staged across successive months so the
pattern response can be measured between steps.

Response factor. The response factor rho relates a fractional change in injection to
the resulting change in VRR. It starts at 0.85 for a new pattern and is updated by an
exponential moving average once the post-change VRR is observed.

Approval chain. A drafted change moves analyst then RM then site. The site engineer is
the only role that may mark a change executed, and execution is recorded in the
adjustment history with the pattern, the date and the recommended surface rate.
"""

# Question → a phrase the retrieved chunk MUST contain for the answer to be complete.
# This is the labelled set `retrieval_check` scores against; grow it as real questions
# surface from the chat transcript in `vrr_agent.chat_history`.
PROBES: list[tuple[str, str]] = [
    ("How much can injection change in a single step?", "15%"),
    ("What does the response factor rho start at?", "0.85"),
    ("Who is allowed to mark a change executed?", "site engineer"),
]


# ------------------------------------------------------------- strategies ----
def split_fixed(text: str, size: int = 200, overlap: int = 0) -> list[str]:
    """Boundary-blind: cut every N characters. Fast, reproducible, and the reason
    half-sentences end up in vector indexes."""
    from langchain_text_splitters import CharacterTextSplitter

    return CharacterTextSplitter(separator="", chunk_size=size,
                                 chunk_overlap=overlap).split_text(text)


def split_recursive(text: str, size: int = 400, overlap: int = 60) -> list[str]:
    """Try paragraph, then line, then sentence, then word — take the largest boundary
    that fits. The right default: most of the benefit of semantic chunking, no embeddings.

    The overlap is the cheap insurance: a rule that still straddles a boundary appears
    whole in one of the two chunks."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""]).split_text(text)


def split_semantic(text: str, embed: Callable[[str], list[float]] | None = None,
                   threshold: float = 0.75, max_chars: int = 800) -> list[str]:
    """Break where the TOPIC turns, not where the character count runs out.

    Walks sentence by sentence, keeping the current chunk while consecutive sentences
    stay similar (cosine ≥ threshold) and closing it when the subject changes. Costs one
    embedding call per sentence at ingest — paid once, against every query forever.

    Falls back to `split_recursive` when no embedder is reachable, so the ingest path
    never breaks just because Ollama is down.
    """
    if embed is None:
        try:
            from .knowledge_ingest import embed as local_embed
            embed = local_embed
        except Exception:
            return split_recursive(text)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(sentences) < 2:
        return [text.strip()] if text.strip() else []
    try:
        vectors = [embed(s) for s in sentences]
    except Exception:                          # embedder unreachable mid-run
        return split_recursive(text)

    chunks, current = [], [sentences[0]]
    for i in range(1, len(sentences)):
        same_topic = _cosine(vectors[i - 1], vectors[i]) >= threshold
        if same_topic and len(" ".join(current)) + len(sentences[i]) <= max_chars:
            current.append(sentences[i])
        else:
            chunks.append(" ".join(current))
            current = [sentences[i]]
    chunks.append(" ".join(current))
    return chunks


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


STRATEGIES: dict[str, Callable[[str], list[str]]] = {
    "fixed": split_fixed, "recursive": split_recursive, "semantic": split_semantic,
}


def split_documents(docs: list[Document], strategy: str = "recursive",
                    **kw: Any) -> list[Document]:
    """Chunk `Document`s, carrying metadata onto every chunk and numbering them.

    The metadata is the point: a chunk that loses `file_name`/`page` cannot be cited,
    and an uncitable chunk is one the RAG answer has to either drop or fake.
    """
    split = STRATEGIES.get(strategy)
    if split is None:
        raise ValueError(f"unknown strategy {strategy!r}; "
                         f"have {', '.join(STRATEGIES)}")
    out: list[Document] = []
    for d in docs:
        pieces = split(d.page_content, **kw) if kw else split(d.page_content)
        for i, piece in enumerate(pieces):
            meta = dict(d.metadata)
            meta.update({"chunk": i, "chunks_in_doc": len(pieces),
                         "chunk_strategy": strategy})
            out.append(Document(page_content=piece, metadata=meta))
    return out


# ------------------------------------------------- measuring, not eyeballing ----
def boundary_quality(chunks: list[str]) -> dict[str, Any]:
    """Cheap structural check, no embeddings: what fraction of chunks end mid-sentence,
    and how uneven are they. Catches an obviously bad splitter in milliseconds."""
    if not chunks:
        return {"chunks": 0}
    clean = sum(1 for c in chunks if c.strip().endswith((".", "!", "?", ":")))
    sizes = [len(c) for c in chunks]
    return {"chunks": len(chunks),
            "ends_on_sentence": round(clean / len(chunks), 2),
            "min": min(sizes), "max": max(sizes),
            "avg": round(sum(sizes) / len(sizes))}


def retrieval_check(chunks: list[str], probes: list[tuple[str, str]] = PROBES,
                    k: int = 2, embed: Callable[[str], list[float]] | None = None
                    ) -> dict[str, Any]:
    """THE chunking test: does the right chunk come back for a real question?

    For each labelled (question, must-contain phrase): embed the question, rank the
    chunks by cosine, and check whether a chunk containing the phrase is in the top-k.

      recall@k  fraction of questions whose answer chunk made the top-k — the number
                that decides whether a chunking change shipped an improvement
      MRR       1/rank of the first correct chunk — rewards ranking it FIRST, which is
                what matters when only 3-4 chunks fit in the prompt

    Needs an embedder (local Ollama by default); returns `{"ok": False}` without one so
    it can sit in a test suite that runs offline.
    """
    if embed is None:
        try:
            from .knowledge_ingest import embed as local_embed
            embed = local_embed
        except Exception as e:
            return {"ok": False, "reason": f"no embedder: {e}"}
    try:
        chunk_vectors = [embed(c) for c in chunks]
    except Exception as e:
        return {"ok": False, "reason": f"embedding failed: {e}"}

    hits, reciprocal, detail = 0, 0.0, []
    for question, needle in probes:
        qv = embed(question)
        ranked = sorted(range(len(chunks)),
                        key=lambda i: _cosine(qv, chunk_vectors[i]), reverse=True)
        rank = next((r + 1 for r, i in enumerate(ranked)
                     if needle.lower() in chunks[i].lower()), None)
        if rank and rank <= k:
            hits += 1
        reciprocal += 1 / rank if rank else 0.0
        detail.append({"question": question, "needle": needle, "rank": rank,
                       "top_chunk": chunks[ranked[0]][:80] + "…"})
    n = len(probes)
    return {"ok": True, "chunks": len(chunks), f"recall@{k}": round(hits / n, 2),
            "mrr": round(reciprocal / n, 2), "detail": detail}


def compare(text: str = SAMPLE, k: int = 2) -> dict[str, Any]:
    """Run every strategy over the same text and score them the same way."""
    report: dict[str, Any] = {}
    for name, split in STRATEGIES.items():
        chunks = split(text)
        report[name] = {"structure": boundary_quality(chunks),
                        "retrieval": retrieval_check(chunks, k=k)}
    return report


if __name__ == "__main__":
    print("=== bad vs good chunking, on the same VRR procedure text ===\n")
    for name, split in STRATEGIES.items():
        chunks = split(SAMPLE)
        q = boundary_quality(chunks)
        print(f"{name:>10}: {q['chunks']} chunks | avg {q['avg']} chars "
              f"| {q['ends_on_sentence']:.0%} end on a sentence boundary")

    print("\n--- the 15% rule, as each strategy leaves it ---")
    for name, split in STRATEGIES.items():
        holder = next((c for c in split(SAMPLE) if "15%" in c), None)
        print(f"{name:>10}: {holder.strip()[:150] if holder else '(lost)'}…")

    print("\n=== retrieval check (recall@2 + MRR over labelled probes) ===")
    for name, scored in compare().items():
        r = scored["retrieval"]
        if not r.get("ok"):
            print(f"{name:>10}: skipped — {r['reason']}")
            continue
        print(f"{name:>10}: recall@2 {r['recall@2']}  MRR {r['mrr']}  "
              f"({r['chunks']} chunks)")
        for d in r["detail"]:
            print(f"            rank {d['rank'] or '—'}  {d['question']}")
