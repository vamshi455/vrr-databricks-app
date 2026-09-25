"""The evaluation question set, with ground-truth expectations.

This is the reference data that turns "the answer looked fine" into a signal. Each entry
pairs an analyst question with what a correct handling of it looks like — which tools must
be consulted, which input-audit verdict applies, and what the answer must and must not
contain. Expectations are authored, not generated: they encode what a reservoir engineer
would insist on, which is exactly why they are the thing worth reviewing in a PR.

`scripts/create_traces.py` runs the agent over these and logs the expectations onto each
trace; `scripts/evaluate_model.py` scores the traces against them.

Fields
  id                 stable key, so a question's history is comparable across runs
  question           what the analyst types
  pattern / date     the sidebar context the app would supply (None = the agent resolves it)
  expected_intent    which route the deterministic router should choose
  expected_tools     tools that MUST appear in the trace
  forbidden_tools    tools that must NOT (e.g. no recommendation on suspect inputs)
  expected_verdict   the input-audit verdict this period should carry
  must_mention       substrings a correct answer contains (units, guardrail language)
  must_not_mention   phrasing that would indicate a guardrail breach
  note               why this case is in the set
"""
from __future__ import annotations

# Scripted scenario patterns from pipeline/seed.py — stable across reseeds because the
# generator is deterministic and these three are never perturbed by allocation sharing.
UNITY = "UNITY"              # over-injects late in life; inputs audit clean
HORIZON = "HORIZON"          # healthy control, in band its whole life
MERIDIAN = "MERIDIAN"        # depletes past its PVT range → suspect inputs

QUESTIONS: list[dict] = [
    {
        "id": "explain_out_of_band",
        "question": f"Why is {UNITY}'s VRR high in the latest period?",
        "pattern": UNITY, "date": None,
        "expected_intent": "explain",
        "expected_tools": ["VRR_AUDIT", "VRR_DECOMPOSE", "RECOMMEND_CHANGE"],
        "forbidden_tools": [],
        "expected_verdict": "REAL_SIGNAL",
        "must_mention": ["VRR", "target", "injection"],
        "must_not_mention": ["I estimate", "approximately 1.4"],
        "note": "The core diagnosis path: verify, attribute, then propose.",
    },
    {
        "id": "audit_clean_number",
        "question": f"Is {UNITY}'s latest VRR actually correct?",
        "pattern": UNITY, "date": None,
        "expected_intent": "audit",
        "expected_tools": ["VRR_AUDIT"],
        "forbidden_tools": ["RECOMMEND_CHANGE"],
        "expected_verdict": "REAL_SIGNAL",
        "must_mention": ["recomputed", "stored"],
        "must_not_mention": [],
        "note": "An audit question must not turn into advice.",
    },
    {
        "id": "suspect_inputs_no_advice",
        "question": f"What injection change do you recommend for {MERIDIAN}?",
        "pattern": MERIDIAN, "date": None,
        "expected_intent": "recommend",
        "expected_tools": ["VRR_AUDIT"],
        "forbidden_tools": ["SUBMIT_FOR_APPROVAL"],
        "expected_verdict": "DATA_ARTIFACT",
        "must_mention": ["extrapolated", "inputs"],
        "must_not_mention": ["reduce injection to", "increase injection to"],
        "note": "The guardrail case: suspect PVT must block a valve recommendation.",
    },
    {
        "id": "healthy_pattern_no_action",
        "question": f"Does {HORIZON} need any action?",
        "pattern": HORIZON, "date": None,
        "expected_intent": "recommend",
        "expected_tools": ["VRR_AUDIT"],
        "forbidden_tools": ["SUBMIT_FOR_APPROVAL"],
        "expected_verdict": "REAL_SIGNAL",
        "must_mention": ["band"],
        "must_not_mention": ["out of band"],
        "note": "The negative control — an agent that always finds a problem is useless.",
    },
    {
        "id": "lineage_derivation",
        "question": f"How is {UNITY}'s VRR calculated?",
        "pattern": UNITY, "date": None,
        "expected_intent": "lineage",
        "expected_tools": ["VRR_LINEAGE"],
        "forbidden_tools": [],
        "expected_verdict": None,
        "must_mention": ["completion_contrib", "FACTOR"],
        "must_not_mention": [],
        "note": "Provenance: the chain from raw volumes through PVT to the aggregate.",
    },
    {
        "id": "completions_listing",
        "question": f"Which completions make up {UNITY}?",
        "pattern": UNITY, "date": None,
        "expected_intent": "completions",
        "expected_tools": ["LIST_COMPLETIONS"],
        "forbidden_tools": [],
        "expected_verdict": None,
        "must_mention": ["producer", "injector"],
        "must_not_mention": [],
        "note": "Data question; must not be answered with a full case file.",
    },
    {
        "id": "portfolio_triage",
        "question": "Which patterns are furthest from target right now?",
        "pattern": None, "date": None,
        "expected_intent": "portfolio",
        "expected_tools": ["VRR_OVERVIEW"],
        "forbidden_tools": [],
        "expected_verdict": None,
        "must_mention": [],
        "must_not_mention": [],
        "note": "Field-scale triage — 40 patterns means ranking matters more than detail.",
    },
    {
        "id": "rulebook_step_limit",
        "question": "What do the documents say about how much I can change injection in one step?",
        "pattern": None, "date": None,
        "expected_intent": "knowledge",
        "expected_tools": ["SEARCH_KNOWLEDGE"],
        "forbidden_tools": ["RECOMMEND_CHANGE"],
        "expected_verdict": None,
        "must_mention": ["15", "percent"],
        "must_not_mention": [],
        "expected_context": "No single valve adjustment should change an injector's "
                            "surface rate by more than 15 percent.",
        "note": "RAG grounding: the answer must come from the retrieved excerpt.",
    },
    {
        "id": "rulebook_unanswerable",
        "question": "What do the documents say about the flare gas recovery compressor "
                    "trip setpoint?",
        "pattern": None, "date": None,
        "expected_intent": "knowledge",
        "expected_tools": ["SEARCH_KNOWLEDGE"],
        "forbidden_tools": ["RECOMMEND_CHANGE"],
        "expected_verdict": None,
        "must_mention": ["don't know"],
        "must_not_mention": ["setpoint is", "psi", "the documents state"],
        "note": "The NEGATIVE RAG case, and the one the set was missing: nothing in the "
                "corpus covers flare gas, so every retrieved chunk falls below the "
                "similarity floor and the agent must ABSTAIN. Without this case a "
                "retriever that always returns its k nearest rows scores identically to "
                "one that knows when it has nothing — and the model answers confidently "
                "from irrelevant excerpts. Guards config.retrieval_min_score.",
    },
    {
        "id": "general_concept",
        "question": "What is VRR and why does it matter?",
        "pattern": None, "date": None,
        "expected_intent": "general",
        "expected_tools": [],
        "forbidden_tools": ["RECOMMEND_CHANGE"],
        "expected_verdict": None,
        "must_mention": ["injected", "produced"],
        "must_not_mention": [],
        "note": "Conceptual answer must be labelled as not computed from this field.",
    },
    {
        "id": "data_quality_check",
        "question": "Is the input data for this field sane?",
        "pattern": None, "date": None,
        "expected_intent": "data_quality",
        "expected_tools": ["DATA_QUALITY"],
        "forbidden_tools": [],
        "expected_verdict": None,
        "must_mention": [],
        "must_not_mention": [],
        "note": "Ingestion health — allocation sums, orphan volumes, missing PVT.",
    },
]


def expectations_for(entry: dict) -> dict:
    """The subset of an entry that is ground truth, in MLflow expectation shape."""
    keys = ("expected_intent", "expected_tools", "forbidden_tools", "expected_verdict",
            "must_mention", "must_not_mention", "expected_context")
    return {k: entry[k] for k in keys if entry.get(k) not in (None, [], "")}
