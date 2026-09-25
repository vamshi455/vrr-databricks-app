# Meridian Petroleum workbench — Portfolio

_Generated from `core/help_topics.py` by `make guide`. Edit that file and re-run; do not edit this document by hand — it is overwritten._

## The Portfolio view

**Portfolio — where to look first.**

Every pattern, ranked by how far its VRR sits from its target. Read it as a triage queue,
not a report: the top row is the pattern most worth your attention today.

- **VRR** is injected reservoir volume ÷ produced reservoir volume. 1.0 means balanced.
- **verdict** compares VRR against that pattern's own target band, not a global 1.0.
- The **inputs** column flags `suspect` when any PVT behind the number was *extrapolated*
  rather than measured. A suspect pattern must be audited before any valve change is
  considered — the figure may be an artefact of the input, not the reservoir.

Click a pattern to select it; the Report, Lineage and chat views all follow that
selection, as does the Pattern dropdown in the left rail.
