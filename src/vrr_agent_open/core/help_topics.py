"""How to use this workbench — answered deterministically, never generated.

The agent already refuses to invent a *number*. This module extends the same rule to the
application itself: "how do I approve a change?" is answered from a written table, not by
a 7B model recalling what a petroleum workbench probably looks like. That failure mode is
worse than it sounds — a fabricated number gets caught by `core.faithfulness`, but
fabricated UI ("click the Export button in the top right") passes every check the project
has, because it makes no numeric claim at all. The user then hunts for a button that does
not exist and concludes the tool is broken.

So app questions are routed here first (`agent/chat.py::_help_answer`). Retrieval over an
ingested user guide is the FALLBACK for the long tail, not the primary path.

Pure — no I/O, no model, no database — so every answer below is unit-tested against the
code it describes (`tests/test_help_topics.py` asserts the view names and role names here
still match `web/src/App.tsx` and `api/routes_approvals.py`).

Keeping it honest over time: this file states what the app does today. When a view
changes, this changes with it, and the test that compares role names to
`APPROVER_FOR_STAGE` fails loudly if the approval chain is re-wired without it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The views in the left rail, in their order there. Named once so a rename shows up in
# one place rather than in nine answer strings.
VIEWS = ("Portfolio", "Report", "Lineage & audit", "Approvals", "Knowledge",
         "Architecture")


@dataclass(frozen=True)
class Topic:
    """One answerable question about the application."""

    id: str
    view: str                       # which view it concerns, or "" for app-wide
    title: str
    keywords: tuple[str, ...]       # any one of these, plus an app noun, selects it
    body: str                       # markdown, shown verbatim — no model touches it
    see_also: tuple[str, ...] = field(default=())


# Words that make a question about the SOFTWARE rather than about the reservoir. A
# question needs one of these before anything here can answer it, which is what stops
# "how is VRR calculated" (a lineage question about the physics) being swallowed by
# "how do I read the Lineage view" (a question about a screen).
APP_NOUNS = (
    "view", "screen", "page", "tab", "button", "click", "drag", "panel", "board",
    "lane", "sidebar", "menu", "workbench", "app", "application", "ui", "interface",
    "dashboard", "navigate", "sign in", "log in", "login", "sign out", "upload",
    "drawer", "chat window", "this tool", "the tool", "get started", "how do i use",
    "how to use", "where do i", "where can i", "how do i find", "walk me through",
    # Board vocabulary. "zone" and "card" were both missing, which meant the most
    # natural way to ask the question this feature exists for — "how do I move a card
    # from the analyst zone to the RM zone?" — fell through to `explain` and came back
    # as a list of patterns. People describe a swim-lane board in the words they know
    # from other tools, not in the words the code uses.
    "card", "zone", "column", "swim lane", "swimlane", "kanban", "scrum",
)

# Phrasings that are unambiguously about using the app even without an app noun.
STRONG_HELP = (
    "how do i use", "how to use", "help me use", "what can this do",
    "what can you do", "what can i do here", "getting started", "get started",
    "show me around", "walk me through the app", "how does this app work",
)


TOPICS: tuple[Topic, ...] = (
    Topic(
        id="overview", view="", title="What this workbench is, and the six views",
        keywords=("overview", "what is this", "what can", "getting started", "get started",
                  "show me around", "how does this app", "what does this app", "views",
                  "navigate", "sections", "menu", "sidebar", "rail", "architecture",
                  "how do i use this", "use this app", "use the app", "using this app",
                  "what can you do", "what can i do"),
        body="""**Meridian Petroleum — VRR Reasoning & Lineage.** It answers one question:
is each waterflood pattern replacing the volume it produces, and if not, what should be
done about it.

Six views, in the left rail, in the order you would actually use them:

| View | The question it answers |
|---|---|
| **Portfolio** | Where do I look first? Every pattern ranked by distance from target. |
| **Report** | What moved, and what should be done? Trend, attribution, the well diagram, a drafted action. |
| **Lineage & audit** | Do I believe this number? The derivation as a graph, plus a recompute from raw. |
| **Approvals** | Who signs it off? A swim-lane board, draft → analyst → RM → site → executed. |
| **Knowledge** | What may the agent read? Upload and approve documents into the search index. |
| **Architecture** | What runs when I ask? The whole system as a clickable map, with live counters. |

The chat drawer (bottom-right, "Ask the agent") is available beside every view and knows
which pattern and period you are looking at.

**The rule underneath all of it:** every number comes from deterministic code in `core/`.
The model chooses tools and phrases results; it never does arithmetic and never gets the
last word on a figure.""",
        see_also=("signin", "chat"),
    ),
    Topic(
        id="portfolio", view="Portfolio", title="The Portfolio view",
        keywords=("portfolio", "first view", "home", "landing", "rank", "ranked",
                  "off target", "which pattern", "overview table", "look first"),
        body="""**Portfolio — where to look first.**

Every pattern, ranked by how far its VRR sits from its target. Read it as a triage queue,
not a report: the top row is the pattern most worth your attention today.

- **VRR** is injected reservoir volume ÷ produced reservoir volume. 1.0 means balanced.
- **verdict** compares VRR against that pattern's own target band, not a global 1.0.
- The **inputs** column flags `suspect` when any PVT behind the number was *extrapolated*
  rather than measured. A suspect pattern must be audited before any valve change is
  considered — the figure may be an artefact of the input, not the reservoir.

Click a pattern to select it; the Report, Lineage and chat views all follow that
selection, as does the Pattern dropdown in the left rail.""",
        see_also=("report", "suspect"),
    ),
    Topic(
        id="report", view="Report", title="The Report view",
        keywords=("report", "trend", "chart", "attribution", "driver", "what moved",
                  "decompose", "diagram", "schematic", "well layout", "pattern diagram",
                  "narrative", "draft", "recommendation"),
        body="""**Report — what moved, and what to do about it.**

Four things, top to bottom:

1. **The trend chart** — monthly VRR against the target band, so drift is visible as a
   shape rather than as a number.
2. **Attribution** — an exact LMDI decomposition of the change between two periods
   (`core/decompose.py`). It says *which term* moved the VRR: water injection, oil
   production, gas, or a formation-volume-factor change. The shares sum to the total, so
   nothing is hand-waved into "other".
3. **The pattern diagram** — the wells placed from their contribution factors, with the
   canonical shape named (five-spot, seven-spot, nine-spot, line drive, or irregular).
   **It is a schematic and says so on its face**: this database holds contribution
   factors, not coordinates, so distance from the injector is allocation, never feet.
4. **The drafted action** — a physics-computed, safety-clamped recommendation. It is a
   draft. Submitting it puts it on the Approvals board at stage `draft`; it changes
   nothing on its own.""",
        see_also=("approvals", "suspect", "lineage"),
    ),
    Topic(
        id="lineage", view="Lineage & audit", title="The Lineage & audit view",
        keywords=("lineage", "audit", "provenance", "where does the number come from",
                  "derivation", "recompute", "graph", "dag", "trust the number",
                  "verify the number", "source table"),
        body="""**Lineage & audit — do I believe this number?**

The derivation drawn as a six-column graph: four raw tables → `core.physics` → one row
per completion → five reservoir terms → two sides → one VRR. Every node carries the value
that actually flowed through it, and hovering traces upstream and lights the formula that
applies.

The **audit** recomputes the stored VRR from the raw rows, right now, and reports the
difference. A match means the stored figure and a fresh computation agree. It also
reports `pvt_methods` — whether the formation volume factors were measured or
extrapolated — and `low_confidence_inputs`.

This is the view to open when someone asks "where did that come from?", because the
answer is a picture of the actual tables and formulas rather than an assertion.""",
        see_also=("suspect", "report"),
    ),
    Topic(
        id="approvals", view="Approvals", title="The Approvals board",
        keywords=("approval", "approve", "board", "swim lane", "swimlane", "lane",
                  "stage", "sign off", "signoff", "reject", "execute", "chain",
                  "kanban", "scrum", "move a card", "drag a card"),
        body="""**Approvals — who signs it off.**

Six lanes, left to right: **draft → analyst → rm → site → executed**, plus **rejected**.

The agent may only ever write `draft`. Every arrow after that is a person, and each lane
advances on a *different* role's sign-off:

| A card sitting in… | …is advanced by | …to |
|---|---|---|
| `draft` | **analyst** | `analyst` |
| `analyst` | **rm** (reservoir manager) | `rm` |
| `rm` | **site** (site engineer) | `site` |
| `site` | **site** | `executed` |

Read the lane name as *"who has already signed"*, not *"who is waiting"*. A card in the
**analyst** lane has the analyst's signature and is waiting on the **RM**.

**To move a card, you must be signed in as the role that owns its current stage.** If the
buttons are missing, that is why — and hiding them is only convenience; the server
refuses a transition your token's role does not own regardless of what the browser shows.

Advancing to `executed` writes `vrr_agent.adjustment_history`, which is the table the ρ
learning loop reads back.""",
        see_also=("move_card", "roles", "signin"),
    ),
    Topic(
        id="move_card", view="Approvals", title="Moving a card between lanes",
        keywords=("move a card", "move the card", "move it", "drag", "drop", "advance",
                  "next lane", "analyst to rm", "analyst lane to", "lane to rm",
                  "rm zone", "analyst zone", "to the rm", "cannot move", "can't move",
                  "won't move", "cannot drag", "can't drag", "greyed", "grayed",
                  "no button", "stuck", "move from"),
        body="""**Two ways to move a card, and one reason it will not move.**

- **Drag it.** Cards you may act on show a grip and lift. Exactly one lane accepts the
  drop — the next one. Every other lane is inert, so the chain cannot be skipped and a
  card cannot be dragged straight to `executed`.
- **Click it, then Approve.** The detail panel shows the narrative, the proposed injector
  changes, and an "Approve → *next*" button. This is also the keyboard route.

**If a card will not lift, you are not the role that owns its stage.** A card in the
`analyst` lane moves to `rm` — and it is the **RM** who moves it, not the analyst. Sign
out and sign in as the RM account.

Rejecting is deliberately not a drag: it is a click, because rejection is terminal.""",
        see_also=("approvals", "roles", "signin"),
    ),
    Topic(
        id="roles", view="", title="Roles, and what each one may do",
        keywords=("role", "which role", "what role", "role can", "my role", "who can",
                  "who is allowed", "permission", "403", "not allowed", "forbidden",
                  "may not do this", "access", "reservoir manager", "site engineer",
                  "data_steward", "steward", "admin", "see mine"),
        body="""**Your role is a signed claim inside your token, not something the browser
chooses.** To act as the RM you sign in as the RM.

| Role | May |
|---|---|
| `analyst` | advance a `draft` to `analyst`; ask the agent |
| `rm` | advance `analyst` → `rm` |
| `site` | advance `rm` → `site`, and `site` → `executed` (the valve change) |
| `data_steward` | upload, preview, approve, reject and remove knowledge documents |
| `admin` | everything a steward may do |

Reading is open to everyone, signed in or not — the portfolio, trends, lineage and audits
are all public on a local workbench. Writing and asking the agent need an account.

A `403` with "role 'x' may not do this" means the server refused, which is the control
working. Hiding a button is convenience; the refusal is what makes it safe.""",
        see_also=("signin", "approvals"),
    ),
    Topic(
        id="knowledge", view="Knowledge", title="The Knowledge view — uploading documents",
        keywords=("knowledge", "upload", "document", "pdf", "ingest", "embed", "index",
                  "attach", "add a file", "my file", "file was rejected",
                  "rejected on upload", "rejected my file", "file type", "too large",
                  "pending review", "not searchable"),
        body="""**Knowledge — what the agent is allowed to read.**

Uploading is **not** the same as indexing. A file you upload lands in quarantine as
`pending_review` and answers nothing. A `data_steward` or `admin` then opens it, reads
the text that was *actually extracted* — which is not what the document looks like in a
viewer — and approves it. Approving embeds it in seconds, and it is askable in the chat
immediately after.

That gate is deliberate: whether a document belongs in a reservoir index is a judgement,
and no model makes it.

Accepted: `.pdf` 25 MB · `.docx` 15 MB · `.csv` 10 MB · `.html`/`.htm`, `.md`/`.txt` 5 MB.

**If your file was refused**, the message says exactly why. The usual causes are an
extension outside that list, contents that do not match the extension (a renamed file),
or identical content already in the index. Any PII found — emails, phones, credentials —
is replaced with `[REDACTED:kind]` before embedding, so the raw value never reaches the
database.

"Remove from index" drops the chunks and keeps the registry row: what was ingested, by
whom, and whether it held PII stays on the record.""",
        see_also=("chat", "roles"),
    ),
    Topic(
        id="chat", view="", title="The chat drawer, and what it will not do",
        keywords=("chat", "ask the agent", "drawer", "chatbot", "bot", "assistant",
                  "agentic", "model picks the tools", "transcript", "clear the chat",
                  "history", "trace"),
        body="""**Ask the agent** — bottom-right, beside every view. It knows which pattern
and period you have selected, so "why is this high?" resolves without you repeating them.

- **Deterministic by default** (~8 s): tools run in a fixed order and the model only
  phrases the result.
- **"Model picks the tools itself"** (~1–2 min): the model drives the tool loop. Slower,
  and more likely to be caught fabricating — in which case you are shown the computed
  answer instead.

Every answer carries its provenance line and a **trace ↗** link to the full span tree in
MLflow: which tools ran, what the model said, what the faithfulness gate decided.

**Runtime questions are probed, not generated.** "Are you connected to an LLM?",
"which model?", "how many patterns?", "are you tracing?" hit the `status` intent and
quote the same live probes `/api/health` uses. A model guessing its own configuration
is a failure mode that path exists to close.

**It abstains.** Ask about something the ingested documents do not cover and it says "I
don't know" *without calling the model at all*, rather than handing you four
confident-looking excerpts that do not answer the question.

**Clear hides, it never deletes.** It records a cutoff for you; other people still see
the full shared transcript and every question remains in MLflow.""",
        see_also=("knowledge", "overview"),
    ),
    Topic(
        id="signin", view="", title="Signing in",
        keywords=("sign in", "signin", "log in", "login", "password", "account",
                  "401", "not authenticated", "token", "expired", "sign out",
                  "who am i", "logged in as"),
        body="""**Reading needs no account.** The portfolio, trends, lineage and audits are
open. Approving anything, uploading a document, or asking the agent needs one.

Sign in from the button at the top right; your name and role then show there, labelled
*"from your token"* — because the role is a signed claim, not a setting.

Demo accounts are created by `make users`, which **prints the password it sets**. There
is one per role: `analyst.demo`, `rm.demo`, `site.demo`, `steward.demo`.

A token lasts 12 hours. If a request that worked earlier starts returning **401**, the
token expired — sign in again. If *every* session dies on a server restart, `VRR_JWT_SECRET`
is unset and each process is signing with a fresh random key.""",
        see_also=("roles",),
    ),
    Topic(
        id="suspect", view="", title="Why a pattern is flagged suspect or off target",
        keywords=("suspect", "flag", "amber", "warning", "extrapolated", "low confidence",
                  "off target", "red", "colour", "color", "why is it highlighted",
                  "what does the icon mean"),
        body="""Three states, one meaning each:

- **on target** (green) — VRR inside this pattern's own target band.
- **off target** (red) — outside it. The number is trusted; the reservoir is not balanced.
- **suspect inputs** (amber) — some PVT behind the figure was **extrapolated** rather than
  measured, because no pressure test existed in the right window. The VRR may be an
  artefact of that input.

The distinction matters: off-target is a reservoir problem and suspect is a *data*
problem, and they need opposite responses. A suspect pattern must be audited — Lineage &
audit, then the recompute — before any valve change is considered, because acting on a
figure derived from an extrapolated FVF means changing the field to fix a spreadsheet.""",
        see_also=("lineage", "portfolio"),
    ),
)

BY_ID = {t.id: t for t in TOPICS}

_WORD = re.compile(r"[a-z0-9]+")


def is_help_question(question: str) -> bool:
    """Is this about the SOFTWARE rather than about the reservoir?

    Deliberately conservative. "How is VRR calculated?" must keep routing to `lineage`,
    which answers it from the real derivation — so a question needs either an unambiguous
    app phrasing, or an app noun alongside a topic keyword. Being too eager here would
    replace a computed answer with a page of prose, which is a downgrade.
    """
    q = (question or "").lower()
    if any(s in q for s in STRONG_HELP):
        return True
    if not any(n in q for n in APP_NOUNS):
        return False
    return bool(match(question))


def match(question: str, limit: int = 1) -> list[Topic]:
    """Rank topics by how much of the question each one explains.

    Scored by the TOTAL length of every keyword that matched, not by the single longest.
    Longest-only was the first attempt and it lost four cases immediately: "which role
    can approve?" scored `approve` (7, under `approvals`) against `role` (4, under
    `roles`) and answered a question about permissions with the board layout. Summing
    rewards a topic that explains several parts of the question, which is a better proxy
    for "this is what they asked about".

    Ties break on the longest single match, then on id, so the ordering is total rather
    than dependent on tuple order.
    """
    q = (question or "").lower()
    scored: list[tuple[int, int, Topic]] = []
    for t in TOPICS:
        matched = [k for k in t.keywords if k in q]
        if matched:
            scored.append((sum(len(k) for k in matched), max(len(k) for k in matched), t))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2].id))
    return [t for _, _, t in scored[:limit]]


def answer(question: str) -> dict | None:
    """The whole deterministic help path: a topic, or None to fall through to retrieval."""
    hits = match(question, limit=2)
    if not hits:
        return None
    top = hits[0]
    related = [BY_ID[r] for r in top.see_also if r in BY_ID]
    return {"topic": top.id, "view": top.view, "title": top.title, "body": top.body,
            "related": [{"id": r.id, "title": r.title} for r in related]}


def index() -> list[dict]:
    """Every topic, for a 'what can I ask?' listing."""
    return [{"id": t.id, "view": t.view, "title": t.title} for t in TOPICS]
