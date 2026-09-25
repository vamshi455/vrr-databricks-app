"""The architecture of this system, as a drawable graph with live numbers on it.

Every other view in this workbench answers a question about the *reservoir*. This one
answers a question about the *application*: what runs when you ask something, where a
figure comes from, and which gate stands between a document and an answer.

Two rules make it worth having rather than being a picture in a README:

* **Every number on it is measured at request time.** `api/routes_architecture.py`
  collects the facts, this module places them on nodes. A box whose fact is missing —
  MLflow down, a table not yet seeded — renders with NO number rather than a stale or
  invented one. `_resolve` returns `None` on any missing key, deliberately refusing to
  format a partial tuple into something that reads as a measurement.
* **The topology lives here, not in React.** Same reason as `core/pattern_layout.py`:
  positions and labels are unit-testable off-DB, and the tests can assert that what the
  diagram *claims* about the code is still true (tool counts, approval stages, view
  names). A diagram that drifts from the system is worse than no diagram, because it is
  believed.

Pure: no I/O, no clock, no randomness. Facts in, geometry out.

Abbreviations used below, expanded once: VRR (voidage replacement ratio), PVT
(pressure-volume-temperature), FVF (formation volume factor), PII (personally
identifiable information), DAG (directed acyclic graph), RAG (retrieval-augmented
generation), RM (reservoir manager), LLM (large language model).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Geometry, in the same 1:1 CSS-pixel space `LineageGraph.tsx` draws in, so the font
# sizes below are also their rendered sizes and stay on the app's type scale (11 = micro,
# 12 = label). Narrower than the browser at desktop width; the wrapper scrolls rather
# than shrinking text under the 11px legibility floor.
CANVAS_W = 1180
NODE_W = 172
NODE_H = 60
GAP_X = 24
GAP_Y = 18
BAND_PAD_X = 16
BAND_HEAD = 34          # room above the first row for the band's own title
BAND_PAD_BOTTOM = 16
BAND_GAP = 22


@dataclass(frozen=True)
class Band:
    """A horizontal lane — one stage of the system, titled."""

    id: str
    title: str
    sub: str


@dataclass(frozen=True)
class NodeSpec:
    """One box. `col`/`row` are its slot in the band; pixels are derived in `build`.

    `keys` names the facts it displays and `template` positions them. Both empty means
    the box is structural (a name, not a measurement) and `static` carries its subtitle —
    used only for things that are true regardless of the data, like a formula.
    """

    id: str
    band: str
    col: int
    row: int
    label: str
    what: str                                   # one sentence, shown on click
    files: tuple[str, ...] = field(default=())  # where it actually lives
    keys: tuple[str, ...] = field(default=())
    template: str = ""
    static: str = ""
    guardrail: str = ""                         # the rule this box enforces, if any


BANDS: tuple[Band, ...] = (
    Band("ingest", "INGEST", "volumes in, one number out — no model touches this path"),
    Band("agent", "THE TURN", "what runs when you ask a question"),
    Band("knowledge", "KNOWLEDGE", "a document cannot be searched until a human approves it"),
    Band("approval", "APPROVAL CHAIN", "a recommendation is a draft until people move it"),
    Band("llmops", "LLM OPS", "every turn is observed offline"),
)

NODES: tuple[NodeSpec, ...] = (
    # ---------------------------------------------------------------- ingest ----
    NodeSpec(
        "raw", "ingest", 0, 0, "vrr_raw",
        what="The landing schema: daily volumes, pattern membership, allocation factors, "
             "pressure readings and PVT tests. Nothing is computed here.",
        files=("pipeline/schema.sql", "pipeline/seed.py"),
        keys=("raw_rows",), template="{:,} daily volume rows",
    ),
    NodeSpec(
        "physics", "ingest", 1, 0, "core.physics",
        what="Converts surface volumes to reservoir barrels using the FVF looked up by "
             "pressure and test date. This is the only arithmetic in the whole system.",
        files=("core/physics.py",),
        static="FACTOR · VOLUME · FVF",
        guardrail="Pure — no database, no model. Unit-tested off-DB.",
    ),
    NodeSpec(
        "curated", "ingest", 2, 0, "vrr_curated",
        what="One row per completion per day, then rolled up to monthly VRR with "
             "volume-weighted average FVFs and a running cumulative.",
        files=("pipeline/build.py",),
        keys=("monthly_rows",), template="{:,} monthly VRR rows",
    ),
    NodeSpec(
        "anomaly", "ingest", 3, 0, "core.anomaly",
        what="Three rules over the curated rows: out of band, drift, and extrapolated "
             "PVT. The third is flagged but never actioned — it is an input problem.",
        files=("core/anomaly.py", "pipeline/anomaly_to_queue.py"),
        keys=("patterns",), template="over {} pattern(s)",
    ),
    NodeSpec(
        "recommend", "ingest", 4, 0, "core.recommend",
        what="Proposes an injection change from the physics and clamps it against the "
             "per-pattern safety limits. The model never sizes a change.",
        files=("core/recommend.py",),
        keys=("safety_limits",), template="{} safety limit row(s)",
        guardrail="Clamped to safety_limits; a clamped recommendation says so.",
    ),
    # ------------------------------------------------------------------ turn ----
    NodeSpec(
        "gateway", "agent", 0, 0, "Chat drawer",
        what="The question arrives at POST /chat, normalised and shape-checked, with a "
             "per-user budget on how many may arrive.",
        files=("api/routes_chat.py", "api/ratelimit.py"),
        keys=("chat_turns",), template="{:,} turn(s) recorded",
        guardrail="NFKC-normalised · 20 chat/min · 5 agentic/5min.",
    ),
    NodeSpec(
        "router", "agent", 1, 0, "Intent router",
        what="Keyword scoring picks the path — lineage, audit, knowledge, help, "
             "status, portfolio and the rest. Deterministic by default; the model only "
             "drives the loop when agentic is asked for.",
        files=("agent/chat.py", "core/status.py"),
        keys=("intents",), template="{} intents",
    ),
    NodeSpec(
        "graph", "agent", 2, 0, "LangGraph loop",
        what="A real StateGraph: plan → tools → gate → repair, with append-only "
             "reducers and a checkpointer so a thread resumes.",
        files=("agent/graph.py", "agent/tools.py"),
        keys=("tools",), template="{} deterministic tools",
    ),
    NodeSpec(
        "gate", "agent", 3, 0, "Faithfulness gate",
        what="Checks every figure in the drafted answer against the tool output that "
             "was supposed to produce it, and rewrites the answer when it cannot.",
        files=("core/faithfulness.py",),
        keys=("gate_repaired",), template="{} repaired or rejected",
        guardrail="Runs on every path to END. The model never gets the last word.",
    ),
    NodeSpec(
        "reply", "agent", 4, 0, "Answer",
        what="Returned with its provenance: which tools ran, which model phrased it, "
             "and what the gate decided.",
        files=("agent/history.py",),
        keys=("llm_turns",), template="{} phrased by a model",
    ),
    # ------------------------------------------------------------- knowledge ----
    NodeSpec(
        "upload", "knowledge", 0, 0, "Upload · quarantine",
        what="A browser upload lands on disk and is embedded by nothing. Role, size, "
             "magic bytes, zip-bomb, traversal and a corpus quota are all checked first.",
        files=("api/routes_knowledge.py", "core/upload_validation.py"),
        keys=("pending_review",), template="{} awaiting review",
        guardrail="data_steward or admin, as a signed claim — never a request field.",
    ),
    NodeSpec(
        "review", "knowledge", 1, 0, "Human approval",
        what="A steward reads the real extracted text and the PII findings, then "
             "approves. Relevance to VRR is a human judgement and stays one.",
        files=("web/src/views/KnowledgeView.tsx",),
        keys=("approved_docs",), template="{} approved doc(s)",
        guardrail="Embedding happens on approval, never on upload.",
    ),
    NodeSpec(
        "embed", "knowledge", 2, 0, "Chunk · redact · embed",
        what="Recursive splitting — chosen because it scored recall@2 of 1.00 against "
             "fixed and semantic — then PII redaction, then embedding.",
        files=("pipeline/text_splitters.py", "pipeline/knowledge_ingest.py"),
        keys=("chunks",), template="{:,} chunk(s) indexed",
    ),
    NodeSpec(
        "corpora", "knowledge", 3, 0, "Two corpora",
        what="Reservoir documents and application help are indexed in one table but "
             "never searched together: top-k is a fixed budget and one would crowd out "
             "the other.",
        files=("core/knowledge.py",),
        keys=("docs_reservoir", "docs_help"), template="{} reservoir · {} help",
    ),
    NodeSpec(
        "floor", "knowledge", 4, 0, "Similarity floor",
        what="Nothing above the floor means the agent says it does not know, WITHOUT "
             "calling the model. The threshold was measured, not guessed.",
        files=("core/knowledge.py",),
        keys=("retrieval_floor",), template="abstain below {:.2f}",
        guardrail="Measured: answerable ≥0.671, off-topic ≤0.564.",
    ),
    # -------------------------------------------------------------- approval ----
    NodeSpec(
        "draft", "approval", 0, 0, "draft",
        what="Where an anomaly-derived recommendation lands. Nobody has looked at it.",
        files=("pipeline/anomaly_to_queue.py",),
        keys=("queue_draft",), template="{} card(s)",
    ),
    NodeSpec(
        "analyst", "approval", 1, 0, "analyst",
        what="Advanced by an analyst — the first human to agree the number is real.",
        files=("api/routes_approvals.py",),
        keys=("queue_analyst",), template="{} card(s)",
    ),
    NodeSpec(
        "rm", "approval", 2, 0, "rm",
        what="Reservoir manager. Signs off that the proposed change is the right one.",
        files=("api/routes_approvals.py",),
        keys=("queue_rm",), template="{} card(s)",
    ),
    NodeSpec(
        "site", "approval", 3, 0, "site",
        what="The operator who would actually turn the valve.",
        files=("api/routes_approvals.py",),
        keys=("queue_site",), template="{} card(s)",
    ),
    NodeSpec(
        "executed", "approval", 4, 0, "executed",
        what="Written to adjustment_history with the authenticated subject as "
             "approved_by. After the next monthly build, make writeback fills "
             "actual_post_vrr and EMA-updates the response factor (ρ) into pattern_memory.",
        files=("api/routes_approvals.py", "pipeline/outcome_writeback.py"),
        keys=("queue_executed",), template="{} card(s)",
        guardrail="Each hop requires its own role; the chain cannot be skipped.",
    ),
    # --------------------------------------------------------------- llm ops ----
    NodeSpec(
        "trace", "llmops", 0, 0, "MLflow traces",
        what="Every turn is traced, including the retriever span for the pgvector "
             "search. Off means the process started before MLFLOW_TRACKING_URI was set.",
        files=("agent/tracing.py",),
        keys=("tracing",), template="{}",
    ),
    NodeSpec(
        "scorers", "llmops", 1, 0, "Deterministic scorers",
        what="Trace scorers that need no model — did a figure have a tool span behind "
             "it, was the right tool called, did the gate pass.",
        files=("evaluation/",),
        keys=("scorers_deterministic",), template="{} scorer(s)",
        guardrail="Where a judge and one of these disagree, this one is right.",
    ),
    NodeSpec(
        "judges", "llmops", 2, 0, "LLM judges",
        what="Two judges read the final answer; grounded_in_documents still walks the "
             "retriever span. Verdicts are treated as UNMEASURED until the next eval "
             "run — shown here rather than hidden, because a scoreboard with a broken "
             "column is worse than a gap.",
        files=("evaluation/",),
        keys=("judges_state",), template="{}",
        guardrail="Not a quality bar. See CLAUDE.md before quoting these.",
    ),
    NodeSpec(
        "identity", "llmops", 3, 0, "Identity",
        what="OAuth2 password grant to a JWT bearer. The role is a signed claim, so a "
             "caller cannot pick its own.",
        files=("api/auth.py",),
        keys=("users",), template="{} account(s)",
        guardrail="Reads are public unless VRR_SHARE=1; writes and chat need a token.",
    ),
    NodeSpec(
        "model", "llmops", 4, 0, "Narrator",
        what="Phrases answers and, in agentic mode, chooses tools. It never computes a "
             "figure, and everything still runs with it switched off.",
        files=("agent/llm.py", "agent/providers.py"),
        keys=("llm",), template="{}",
    ),
)

# Where the work actually flows. Cross-band edges are the interesting ones: they are the
# claims a reader would otherwise have to take on trust.
EDGES: tuple[tuple[str, str, str], ...] = (
    ("raw", "physics", ""),
    ("physics", "curated", ""),
    ("curated", "anomaly", ""),
    ("anomaly", "recommend", ""),
    ("gateway", "router", ""),
    ("router", "graph", ""),
    ("graph", "gate", ""),
    ("gate", "reply", ""),
    ("upload", "review", ""),
    # No label: the gap between two boxes in a row is 24px and any caption longer than
    # that is drawn over the boxes either side. The band's own subtitle already says
    # a document is not searchable until a human approves it.
    ("review", "embed", ""),
    ("embed", "corpora", ""),
    ("corpora", "floor", ""),
    ("draft", "analyst", ""),
    ("analyst", "rm", ""),
    ("rm", "site", ""),
    ("site", "executed", ""),
    ("trace", "scorers", ""),
    ("scorers", "judges", ""),
    # ---- across bands ----
    ("curated", "graph", "tools read curated"),
    ("recommend", "draft", "queued"),
    ("floor", "graph", "or abstain"),
    ("reply", "trace", "every turn"),
    ("model", "graph", "phrases only"),
    ("identity", "upload", "signed role"),
    # Same claim, second target — labelled once. The duplicate landed on top of the
    # `analyst` box, and two identical captions on one map read as two different facts.
    ("identity", "analyst", ""),
)

_BY_ID = {n.id: n for n in NODES}
_BAND_IDS = {b.id for b in BANDS}


def _resolve(spec: NodeSpec, facts: dict) -> str | None:
    """The subtitle for one box, or None when it cannot be stated truthfully.

    Missing means missing. Formatting `{} of {}` with half its arguments present would
    put a number on screen that no query produced, which is the exact failure the rest of
    this codebase is built to prevent.
    """
    if spec.static:
        return spec.static
    if not spec.keys:
        return None
    values = [facts.get(k) for k in spec.keys]
    if any(v is None for v in values):
        return None
    try:
        return spec.template.format(*values)
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _band_rows(band_id: str) -> int:
    return max((n.row for n in NODES if n.band == band_id), default=0) + 1


def build(facts: dict | None = None) -> dict:
    """Place every box and lane, and hang the live facts on them.

    Returns plain dictionaries so the browser renders and never computes — the same
    contract `pattern_layout` and `lineage` already use.
    """
    facts = facts or {}
    bands: list[dict] = []
    nodes: list[dict] = []

    y = 0
    for band in BANDS:
        rows = _band_rows(band.id)
        height = BAND_HEAD + rows * NODE_H + (rows - 1) * GAP_Y + BAND_PAD_BOTTOM
        bands.append({"id": band.id, "title": band.title, "sub": band.sub,
                      "x": 0, "y": y, "w": CANVAS_W, "h": height})

        for spec in (n for n in NODES if n.band == band.id):
            nodes.append({
                "id": spec.id,
                "band": spec.band,
                "label": spec.label,
                "value": _resolve(spec, facts),
                "what": spec.what,
                "files": list(spec.files),
                "guardrail": spec.guardrail,
                "x": BAND_PAD_X + spec.col * (NODE_W + GAP_X),
                "y": y + BAND_HEAD + spec.row * (NODE_H + GAP_Y),
                "w": NODE_W,
                "h": NODE_H,
            })
        y += height + BAND_GAP

    return {
        "canvas": {"w": CANVAS_W, "h": max(y - BAND_GAP, 0)},
        "bands": bands,
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "label": lbl} for a, b, lbl in EDGES],
    }


def fact_keys() -> set[str]:
    """Every fact key the diagram can display — the contract the route must fill."""
    return {k for n in NODES for k in n.keys}
