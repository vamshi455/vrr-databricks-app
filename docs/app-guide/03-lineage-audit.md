# Meridian Petroleum workbench — Lineage & audit

_Generated from `core/help_topics.py` by `make guide`. Edit that file and re-run; do not edit this document by hand — it is overwritten._

## The Lineage & audit view

**Lineage & audit — do I believe this number?**

The derivation drawn as a six-column graph: four raw tables → `core.physics` → one row
per completion → five reservoir terms → two sides → one VRR. Every node carries the value
that actually flowed through it, and hovering traces upstream and lights the formula that
applies.

The **audit** recomputes the stored VRR from the raw rows, right now, and reports the
difference. A match means the stored figure and a fresh computation agree. It also
reports `pvt_methods` — whether the formation volume factors were measured or
extrapolated — and `low_confidence_inputs`.

This is the view to open when someone asks "where did that come from?", because the
answer is a picture of the actual tables and formulas rather than an assertion.
