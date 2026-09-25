# Meridian Petroleum workbench — Report

_Generated from `core/help_topics.py` by `make guide`. Edit that file and re-run; do not edit this document by hand — it is overwritten._

## The Report view

**Report — what moved, and what to do about it.**

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
   nothing on its own.
