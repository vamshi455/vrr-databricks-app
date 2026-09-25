"""What similarity floor separates "the docs answer this" from "they don't"?

`config.retrieval_min_score` decides when the agent abstains. Guessing it is worse than
useless: with `nomic-embed-text`, two unrelated sentences still score ~0.40-0.56, so an
intuitive 0.35 admits everything and the abstain path never fires — the agent hands the
model three irrelevant excerpts and it answers from them.

So measure. Two labelled question sets against the LIVE index:
  ANSWERABLE  questions the ingested corpus does cover  → want these ABOVE the floor
  OFF_TOPIC   questions it plainly does not             → want these BELOW it

The floor goes in the gap. A NEGATIVE gap means the sets overlap and no threshold can
separate them — that is a retrieval problem (wrong chunking, wrong embedder), not a
tuning problem, and no amount of moving the number will fix it.

Re-run after changing the embedding model, the chunking strategy, or the corpus.

Run: `make floor`
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from vrr_agent_open.config import load_config
from vrr_agent_open.pipeline.knowledge_ingest import search

CFG = load_config()

ANSWERABLE = [
    "how much can I change injection in one step?",
    "what does the response factor rho start at?",
    "who approves an injection change?",
    "how is PVT data validated?",
    "what is the allocation standard for a pattern?",
]
OFF_TOPIC = [
    "flare gas recovery compressor trip setpoint",
    "what is the crew rotation schedule for the drilling rig?",
    "how do I file an expense report?",
    "what is the tensile strength of the casing steel?",
    "recipe for sourdough bread",
]


def top_scores(questions: list[str]) -> list[tuple[str, float]]:
    out = []
    for q in questions:
        hits = search(q, 3, min_score=0.0)          # unfiltered: we are measuring
        out.append((q, hits[0]["score"] if hits else 0.0))
    return out


if __name__ == "__main__":
    answerable = top_scores(ANSWERABLE)
    off_topic = top_scores(OFF_TOPIC)

    print("ANSWERABLE (want above the floor)")
    for q, s in answerable:
        print(f"  {s:.3f}  {q}")
    print("\nOFF-TOPIC (want below the floor)")
    for q, s in off_topic:
        print(f"  {s:.3f}  {q}")

    lo = min(s for _, s in answerable)
    hi = max(s for _, s in off_topic)
    gap = lo - hi
    print(f"\nanswerable min: {lo:.3f}   off-topic max: {hi:.3f}   gap: {gap:+.3f}")
    if gap <= 0:
        print("\n⚠️  The sets OVERLAP — no threshold separates them. Fix retrieval "
              "(chunking, embedding model), not the number.")
    else:
        print(f"suggested VRR_RETRIEVAL_MIN_SCORE = {(lo + hi) / 2:.2f}   "
              f"(currently {CFG.retrieval_min_score})")
