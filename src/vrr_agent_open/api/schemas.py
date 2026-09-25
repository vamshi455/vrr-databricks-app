"""Request bodies for the write endpoints.

Only the WRITES are typed. Read endpoints return the tool payloads verbatim — the whole
value of `agent/tools.py` is that its output already carries provenance (`sources`,
`run_id`, `pvt_methods`, `formulas`), and re-declaring those shapes here would create a
second definition of truth that silently drifts from the first.
"""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, Field, field_validator

# Identifiers that reach SQL as parameters and reach the UI as text. Constrained by
# SHAPE, not by a lookup, so a malformed value fails as a 422 naming the field instead of
# a 502 from somewhere deep in a tool. (Both are parameterised queries, so neither was an
# injection risk — this is about telling the caller which input was wrong.)
PATTERN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# C0/C1 controls minus tab and newline. A question is prose typed by a person; a NUL or
# an ANSI escape in it is either a mistake or an attempt to corrupt a log line, a
# terminal, or the transcript the next reviewer reads.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    pattern: str | None = None          # sidebar context: pattern the analyst is looking at
    date: str | None = None             # sidebar context: period under review, YYYY-MM-DD
    agentic: bool = False               # let the model drive the tool loop itself
    persist: bool = True                # write the turn to vrr_agent.chat_history
    # NOTE: no `asked_by` — the asker is the authenticated user. A client-supplied
    # identity in a shared transcript is a signature anyone can forge.

    @field_validator("question")
    @classmethod
    def _clean_question(cls, v: str) -> str:
        """Normalise, strip controls, and refuse a question that is only whitespace.

        NFKC first: without it "ｗｈｙ" and "why" are different strings to the intent
        router, which keys on words (CLAUDE.md operating rule 8) — so a full-width or
        combining-character variant silently routes to a different code path than the
        same question typed normally.
        """
        v = unicodedata.normalize("NFKC", v)
        v = _CONTROL.sub("", v).strip()
        if not v:
            raise ValueError("question is empty once whitespace is stripped")
        if len(v) > 2000:
            raise ValueError("question exceeds 2000 characters")
        return v

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not PATTERN_ID_RE.match(v):
            raise ValueError("pattern must be 1-64 chars of A-Z, a-z, 0-9, _ or -")
        return v

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if not ISO_DATE_RE.match(v):
            raise ValueError("date must be YYYY-MM-DD")
        return v


class SubmitRequest(BaseModel):
    date: str
    # no `submitted_by`: it is the token's subject.


# StageRequest is gone on purpose. Advancing or rejecting takes NO body at all: the
# actor is the token's subject and the role is its signed claim, so there was nothing
# left for the client to send — and anything it could send would be a claim about
# itself, which is exactly what this refactor removed.
