# Meridian Petroleum workbench — Approvals

_Generated from `core/help_topics.py` by `make guide`. Edit that file and re-run; do not edit this document by hand — it is overwritten._

## The Approvals board

**Approvals — who signs it off.**

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
learning loop reads back.

## Moving a card between lanes

**Two ways to move a card, and one reason it will not move.**

- **Drag it.** Cards you may act on show a grip and lift. Exactly one lane accepts the
  drop — the next one. Every other lane is inert, so the chain cannot be skipped and a
  card cannot be dragged straight to `executed`.
- **Click it, then Approve.** The detail panel shows the narrative, the proposed injector
  changes, and an "Approve → *next*" button. This is also the keyboard route.

**If a card will not lift, you are not the role that owns its stage.** A card in the
`analyst` lane moves to `rm` — and it is the **RM** who moves it, not the analyst. Sign
out and sign in as the RM account.

Rejecting is deliberately not a drag: it is a click, because rejection is terminal.
