"""Surrogate keys that look like ingested source keys (pure, no I/O).

Production `ID_PATTERN` / `ID_COMPLETION` are opaque machine keys, not `PAT-001`. This
module mints 16-character uppercase-hex IDs, **deterministically** from the parts you
give it, so a reseed reproduces the same field byte for byte while still exercising code
paths that must not assume tidy identifiers.

    >>> hex_id("pattern", "UNITY")
    'A19F...'                     # 16 chars, [0-9A-F]

Determinism comes from blake2b over the joined parts (plus an optional namespace salt),
truncated to 8 bytes. Collision risk at this length is ~1 in 2^32 for a few thousand
keys — fine for a demo field and for source keys we do not control.
"""
from __future__ import annotations

import hashlib
import re

ID_CHARS = 16                                   # 8 bytes of digest, hex-encoded
ID_RE = re.compile(r"^[0-9A-F]{16}$")           # matches the CHECK constraint in schema.sql


def hex_id(*parts: object, salt: str = "vrr") -> str:
    """A deterministic 16-char uppercase-hex ID for the given parts."""
    payload = "\x1f".join(str(p) for p in (salt, *parts))
    return hashlib.blake2b(payload.encode(), digest_size=ID_CHARS // 2).hexdigest().upper()


def is_valid(value: object) -> bool:
    """True for a well-formed ID — the same rule the database enforces."""
    return isinstance(value, str) and bool(ID_RE.match(value))


def short(value: str, keep: int = 6) -> str:
    """Display form: first ``keep`` characters + an ellipsis (full ID stays in provenance).

    UIs and narratives need something scannable; audit output never uses this.
    """
    if not isinstance(value, str) or len(value) <= keep:
        return str(value)
    return f"{value[:keep]}…"
