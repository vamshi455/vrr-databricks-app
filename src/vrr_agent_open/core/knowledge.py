"""Knowledge-PDF ingestion helpers: chunking + PII detection (design §5.4A Slice C).

Pure Python (no I/O, no Spark) so it unit-tests off-cluster — same split as
physics.py/recommend.py/anomaly.py. ``11_knowledge_ingest.py`` does the volume
scan / PDF parse / embed; this module owns the deterministic text work:

  chunk_text   paragraph-aware splitting with overlap (embedding-friendly sizes)
  detect_pii   regex detectors for common PII (email, phone, SSN, credit card,
               and named-credential lines) — deterministic, auditable
  redact_pii   replace every match with [REDACTED:<kind>] before embedding, so
               PII never reaches the vector index; the registry records kinds.

The VRR-relevance validation of an upload is deliberately NOT automated — a
human reviewer approves each file in ``knowledge_registry`` first (guardrail:
only VRR-related documents may be embedded).
"""
from __future__ import annotations

import re

MAX_CHUNK_CHARS = 1500   # ~350-400 tokens; comfortable for gte-large embeddings
CHUNK_OVERLAP = 200      # tail of one chunk repeated at the head of the next

# Deterministic PII detectors (kind -> compiled regex). Order matters: the
# card/ssn patterns are checked before the generic phone pattern would eat them.
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("phone", re.compile(r"(?<![\d-])(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\d-])")),
    ("credential", re.compile(r"(?i)\b(password|passwd|api[_ -]?key|secret|token)\b\s*[:=]\s*\S+")),
]


def detect_pii(text: str) -> list[dict]:
    """Return every PII hit as {kind, match} (deduped, order-stable)."""
    out, seen = [], set()
    for kind, pat in PII_PATTERNS:
        for m in pat.finditer(text or ""):
            key = (kind, m.group(0))
            if key not in seen:
                seen.add(key)
                out.append({"kind": kind, "match": m.group(0)})
    return out


def redact_pii(text: str) -> tuple[str, list[str]]:
    """Replace all PII with [REDACTED:<kind>]; return (clean_text, kinds_found)."""
    kinds: list[str] = []
    for kind, pat in PII_PATTERNS:
        if pat.search(text or ""):
            kinds.append(kind)
            text = pat.sub(f"[REDACTED:{kind}]", text)
    return text, kinds


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-aware chunking: pack whole paragraphs up to ``max_chars``; a
    paragraph longer than the budget is hard-split. Consecutive chunks share an
    ``overlap``-char tail so context isn't cut mid-thought."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        while len(p) > max_chars:                    # oversized paragraph: hard split
            if buf:
                chunks.append(buf)
                buf = ""
            cut = p.rfind(" ", max_chars - 200, max_chars)
            cut = cut if cut > 0 else max_chars
            chunks.append(p[:cut].strip())
            p = (p[max(0, cut - overlap):]).strip()  # overlap into the remainder
            if len(p) <= max_chars:
                break
        if not p:
            continue
        if buf and len(buf) + 2 + len(p) > max_chars:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
            buf = (buf + "\n\n" + p).strip() if buf else p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks
