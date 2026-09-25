"""Input-audit gate — is this VRR a real reservoir signal, or a data artifact?

The highest-value guardrail in the system (parent design Slice A, after the IPTC
reservoir-model-assessment idea): **before any recommendation**, audit the inputs that
produced the number. Never adjust a valve because of a PVT-extrapolation bug or a missing
pressure reading.

Pure Python (no I/O) like the rest of ``core/`` — the caller supplies the input state it
gathered from Postgres (``pipeline/input_audit.py`` for the batch job,
``agent.tools.vrr_audit`` for on-demand), and this module decides:

  ``DATA_ARTIFACT``  the inputs are broken → route to the data steward, no valve change
  ``INCONCLUSIVE``   inputs are questionable → investigate, do not act
  ``REAL_SIGNAL``    inputs are trustworthy → diagnose and recommend

Severity drives the verdict: any ``high`` finding is disqualifying; a ``medium`` finding
alone makes the period inconclusive; only a clean audit is actionable.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

DATA_ARTIFACT = "DATA_ARTIFACT"
INCONCLUSIVE = "INCONCLUSIVE"
REAL_SIGNAL = "REAL_SIGNAL"

# PVT lookup labels that mean the FVFs were not measured at this pressure.
LOW_CONFIDENCE_PVT = ("extrapolated", "closest", "none")

RECOMPUTE_TOLERANCE = 1e-6          # |stored − recomputed| above this = a stale build
STALE_ALLOCATION_DAYS = 1095        # allocation untouched for 3 years is worth flagging


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str                   # high | medium | low
    detail: str
    evidence: dict = field(default_factory=dict)


def _verdict_from(findings: list[Finding], n_rows: int) -> str:
    if n_rows <= 0:
        return INCONCLUSIVE                      # nothing to assess
    if any(f.severity == "high" for f in findings):
        return DATA_ARTIFACT
    if any(f.severity == "medium" for f in findings):
        return INCONCLUSIVE
    return REAL_SIGNAL


def assess_inputs(
    *,
    n_rows: int,
    pvt_methods: list | tuple | set = (),
    n_missing_pressure: int = 0,
    n_missing_factor: int = 0,
    n_allocation_over_one: int = 0,
    n_allocated_without_volumes: int = 0,
    recompute_difference: float | None = None,
    allocation_age_days: int | None = None,
    tolerance: float = RECOMPUTE_TOLERANCE,
    stale_allocation_days: int = STALE_ALLOCATION_DAYS,
) -> dict:
    """Assess one pattern-period's inputs. Returns verdict + structured findings.

    Every argument is a count or a measurement the caller read from the data — this
    function invents nothing, so the same inputs always give the same verdict.
    """
    findings: list[Finding] = []
    low = sorted({m for m in pvt_methods if m in LOW_CONFIDENCE_PVT})
    if low:
        findings.append(Finding(
            "pvt_not_measured", "high",
            f"PVT was {', '.join(low)} for at least one contributing row — the formation "
            "volume factors were not measured at this pattern pressure, so the derived "
            "reservoir volumes carry unquantified error.",
            {"pvt_methods": low}))
    if n_missing_pressure:
        findings.append(Finding(
            "missing_pressure", "high",
            f"{n_missing_pressure} row(s) had no pattern pressure in force, so PVT could "
            "not be evaluated at the right pressure.",
            {"rows": n_missing_pressure}))
    if n_missing_factor:
        findings.append(Finding(
            "missing_allocation", "high",
            f"{n_missing_factor} volume row(s) fall outside every contribution-factor "
            "window — production that belongs to no pattern.",
            {"rows": n_missing_factor}))
    if n_allocation_over_one:
        findings.append(Finding(
            "allocation_over_one", "high",
            f"{n_allocation_over_one} completion-window(s) allocate more than 100% of a "
            "completion across patterns — the same volume is being counted twice.",
            {"windows": n_allocation_over_one}))
    if recompute_difference is not None and abs(recompute_difference) > tolerance:
        findings.append(Finding(
            "stale_build", "high",
            f"Recomputing this period from raw gives a different VRR "
            f"(difference {recompute_difference:+.3e}) — the curated layer is stale or the "
            "raw data changed after the build.",
            {"difference": recompute_difference, "tolerance": tolerance}))
    if n_allocated_without_volumes:
        findings.append(Finding(
            "allocated_without_volumes", "medium",
            f"{n_allocated_without_volumes} completion(s) are allocated to this pattern but "
            "reported no volumes in the period — either genuinely shut in, or missing data.",
            {"completions": n_allocated_without_volumes}))
    if allocation_age_days is not None and allocation_age_days > stale_allocation_days:
        findings.append(Finding(
            "stale_allocation", "medium",
            f"The contribution factors in force are {allocation_age_days} days old "
            f"(> {stale_allocation_days}); the allocation may no longer reflect the field.",
            {"age_days": allocation_age_days}))

    verdict = _verdict_from(findings, n_rows)
    return {
        "verdict": verdict,
        "actionable": verdict == REAL_SIGNAL,
        "findings": [asdict(f) for f in findings],
        "n_rows": n_rows,
        "summary": summarize(verdict, findings),
    }


def summarize(verdict: str, findings: list[Finding]) -> str:
    """One line an engineer can read in the queue."""
    if verdict == REAL_SIGNAL:
        return ("Inputs audited clean — measured PVT, pressure and allocation all present; "
                "the VRR move is a real reservoir signal.")
    lead = ("Inputs are unusable for a valve decision" if verdict == DATA_ARTIFACT
            else "Inputs are questionable")
    if not findings:
        return f"{lead} — no contributing rows for this period."
    worst = [f for f in findings if f.severity == "high"] or findings
    return f"{lead}: {worst[0].detail}"


def route_for(verdict: str) -> dict:
    """What the workflow should do with this verdict (used to open the queue item)."""
    if verdict == REAL_SIGNAL:
        return {"action_type": None, "owner_role": "analyst",
                "note": "proceed to diagnose and recommend"}
    if verdict == DATA_ARTIFACT:
        return {"action_type": "fix_inputs", "owner_role": "data_steward",
                "note": "fix the source data and re-run the build; no valve change proposed"}
    return {"action_type": "investigate_inputs", "owner_role": "analyst",
            "note": "audit the inputs before proposing any change"}
