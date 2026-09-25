"""Faithfulness gate — the LLM may only name drivers the decomposition supports.

Pure (no I/O), so it unit-tests off-DB like the rest of ``core/``. The agent graph
runs :func:`check_faithfulness` on the narration *before* it reaches the analyst; a
failing answer is replaced by the deterministic summary rather than shown.

Three violations, all detectable without another model:
  unsupported_driver  the text names a term the decomposition ranks as negligible
  wrong_direction     the text says a term pushed VRR the way the numbers deny
  uncited_number      a VRR-scale figure appears that no tool result produced
"""
from __future__ import annotations

import re

from .decompose import TERM_LABELS

# Phrases that map onto a decomposition term. Order matters (longest first) so
# "water injection" never matches as "water production".
TERM_PHRASES: dict[str, tuple[str, ...]] = {
    "water_inj_res": ("water injection", "injected water", "water inj", "injection water"),
    "gas_inj_res": ("gas injection", "injected gas", "gas inj"),
    "oil_res": ("oil production", "oil rate", "oil volume", "produced oil"),
    "water_res": ("water production", "water cut", "produced water", "water rate"),
    "free_gas_res": ("free gas", "gas production", "produced gas", "gas rate"),
}

NEGLIGIBLE_SHARE = 0.10       # a term below this share of |ΔVRR| may not be "the driver"
# Phrases that turn a mention into a CAUSAL CLAIM. Listing a small term with its true
# share is fine ("gas injection contributed 0.0%"); calling it the cause is not.
DRIVER_CLAIMS = ("driven by", "drove", "due to", "because of", "caused by", "cause of",
                 "main", "primary", "dominant", "key driver", "responsible for",
                 "attributable to", "explained by", "the driver", "result of")
UP_WORDS = ("increase", "increased", "higher", "rise", "rose", "up", "drove up", "raised")
DOWN_WORDS = ("decrease", "decreased", "lower", "fell", "drop", "dropped", "down", "reduced")


def mentioned_terms(text: str) -> set[str]:
    """Decomposition terms the narration explicitly names."""
    low = text.lower()
    return {term for term, phrases in TERM_PHRASES.items()
            if any(p in low for p in phrases)}


def _clauses_about(text: str, term: str) -> list[str]:
    """Clauses (not whole sentences) naming the term.

    Clause-level matters: "water injection fell, pushing VRR up" carries two opposite
    direction words, but only the first one is a claim *about the term*.
    """
    phrases = TERM_PHRASES[term]
    clauses = re.split(r"[.,;:!?\n]|\band\b|\bwhile\b|\bwhereas\b", text)
    return [c for c in clauses if any(p in c.lower() for p in phrases)]


def check_faithfulness(answer: str, decompose: dict | None,
                       *, negligible_share: float = NEGLIGIBLE_SHARE) -> dict:
    """Verify narration against a ``core.decompose.decompose_vrr`` result.

    Returns ``{"ok": bool, "violations": [...], "supported": [...]}``. With no
    decomposition to check against there is nothing to contradict, so it passes —
    the numbers in the text are still tool-sourced by construction.
    """
    if not decompose or not decompose.get("ok"):
        return {"ok": True, "violations": [], "supported": [],
                "note": "no decomposition in context; nothing to verify against"}

    by_term = {d["term"]: d for d in decompose["drivers"]}
    violations, supported = [], []

    for term in mentioned_terms(answer):
        d = by_term.get(term)
        if d is None:
            violations.append({"kind": "unsupported_driver", "term": term,
                               "detail": f"'{TERM_LABELS[term]}' is not a term in the "
                                         "decomposition of this VRR change."})
            continue
        claimed_as_cause = any(
            any(c in clause.lower() for c in DRIVER_CLAIMS)
            for clause in _clauses_about(answer, term))
        if d["share"] < negligible_share and claimed_as_cause:
            violations.append({
                "kind": "unsupported_driver", "term": term,
                "detail": (f"'{TERM_LABELS[term]}' is presented as a cause but accounts "
                           f"for only {d['share']*100:.1f}% of |ΔVRR| — below the "
                           f"{negligible_share*100:.0f}% support threshold.")})
            continue
        # Direction: compare against the term's OWN change when the decomposition
        # reports it (`delta`), else fall back to its effect on VRR.
        moved = d.get("delta")
        if moved is None:
            moved = d["contribution"]
        for c in _clauses_about(answer, term):
            low = c.lower()
            said_up = any(w in low for w in UP_WORDS)
            said_down = any(w in low for w in DOWN_WORDS)
            if said_up == said_down:          # neither, or genuinely ambiguous
                continue
            if (said_up and moved < 0) or (said_down and moved > 0):
                violations.append({
                    "kind": "wrong_direction", "term": term,
                    "detail": (f"Narration has '{TERM_LABELS[term]}' moving the wrong "
                               f"way; it changed by {moved:+,.0f} rb and contributed "
                               f"{d['contribution']:+.4f} VRR.")})
                break
        supported.append(term)

    return {"ok": not violations, "violations": violations, "supported": supported}


def check_numbers(answer: str, allowed: list[float], *, tol: float = 0.005) -> dict:
    """Every decimal figure in the narration must match a tool-produced number.

    Catches the classic failure — a fluent model rounding 1.327 to "about 1.4" or
    inventing a percentage. Integers and years are ignored (they are rarely VRR
    quantities and produce noise); only decimals are checked.

    Two allowances, because both are honest presentation rather than invention:

    * **Sign** — the pattern deliberately excludes a leading ``+``/``-`` (it would also
      swallow ranges and hyphens), so figures are compared by magnitude. Otherwise every
      negative contribution — a production term that fell — reads as uncited.
    * **Presentation rounding** — a figure printed to *n* decimals is accepted when some
      tool value rounds to it at *n* decimals: ``0.56`` may be shown as ``0.6``, and
      ``-0.025477`` as ``0.0255``. Inventing a value still fails, because no tool number
      rounds to it: ``1.327`` never becomes ``1.4``.
    """
    found = [(m, float(m)) for m in re.findall(r"(?<![\w.])\d+\.\d+(?!\d)", answer)]
    magnitudes = [abs(a) for a in allowed]
    bad = []
    for text, value in found:
        places = len(text.split(".")[1])
        if any(abs(value - m) <= max(tol, abs(m) * tol) for m in magnitudes):
            continue
        if any(round(m, places) == value for m in magnitudes):
            continue
        bad.append(value)
    return {"ok": not bad, "uncited": bad, "checked": [v for _, v in found]}
