# Meridian Petroleum workbench — General

_Generated from `core/help_topics.py` by `make guide`. Edit that file and re-run; do not edit this document by hand — it is overwritten._

## What this workbench is, and the six views

**Meridian Petroleum — VRR Reasoning & Lineage.** It answers one question:
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
last word on a figure.

## Roles, and what each one may do

**Your role is a signed claim inside your token, not something the browser
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
working. Hiding a button is convenience; the refusal is what makes it safe.

## The chat drawer, and what it will not do

**Ask the agent** — bottom-right, beside every view. It knows which pattern
and period you have selected, so "why is this high?" resolves without you repeating them.

- **Deterministic by default** (~8 s): tools run in a fixed order and the model only
  phrases the result.
- **"Model picks the tools itself"** (~1–2 min): the model drives the tool loop. Slower,
  and more likely to be caught fabricating — in which case you are shown the computed
  answer instead.

Every answer carries its provenance line and a **trace ↗** link to the full span tree in
MLflow: which tools ran, what the model said, what the faithfulness gate decided.

**It abstains.** Ask about something the ingested documents do not cover and it says "I
don't know" *without calling the model at all*, rather than handing you four
confident-looking excerpts that do not answer the question.

**Clear hides, it never deletes.** It records a cutoff for you; other people still see
the full shared transcript and every question remains in MLflow.

## Signing in

**Reading needs no account.** The portfolio, trends, lineage and audits are
open. Approving anything, uploading a document, or asking the agent needs one.

Sign in from the button at the top right; your name and role then show there, labelled
*"from your token"* — because the role is a signed claim, not a setting.

Demo accounts are created by `make users`, which **prints the password it sets**. There
is one per role: `analyst.demo`, `rm.demo`, `site.demo`, `steward.demo`.

A token lasts 12 hours. If a request that worked earlier starts returning **401**, the
token expired — sign in again. If *every* session dies on a server restart, `VRR_JWT_SECRET`
is unset and each process is signing with a fresh random key.

## Why a pattern is flagged suspect or off target

Three states, one meaning each:

- **on target** (green) — VRR inside this pattern's own target band.
- **off target** (red) — outside it. The number is trusted; the reservoir is not balanced.
- **suspect inputs** (amber) — some PVT behind the figure was **extrapolated** rather than
  measured, because no pressure test existed in the right window. The VRR may be an
  artefact of that input.

The distinction matters: off-target is a reservoir problem and suspect is a *data*
problem, and they need opposite responses. A suspect pattern must be audited — Lineage &
audit, then the recompute — before any valve change is considered, because acting on a
figure derived from an extrapolated FVF means changing the field to fix a spreadsheet.
