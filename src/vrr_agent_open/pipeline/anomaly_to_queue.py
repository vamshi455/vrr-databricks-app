"""Anomaly → action_queue job (the OSS equivalent of 09_anomaly_to_queue).

Scans every pattern's latest period with the deterministic stack (verify → attribute →
classify → recommend → draft, all in ``agent.analyst``) and queues one draft per
pattern at stage ``draft``. Nothing here decides anything a human can't overrule: the
queue is advisory until analyst → RM → site sign-off (``core.approval``).

Idempotent per (pattern, period): re-running replaces the pattern's existing draft for
that period rather than piling up duplicates.

    make queue            # all patterns, latest period
    python -m vrr_agent_open.pipeline.anomaly_to_queue UNITY 2026-04-01
"""
from __future__ import annotations

import sys

from ..agent import analyst as AZ
from ..agent import tools as T


def run(pattern: str | None = None, date: str | None = None) -> list[dict]:
    """Queue drafts for one pattern or all. Returns the queued rows' summaries."""
    patterns = ([pattern] if pattern else
                [p["pattern_id"] for p in T.list_patterns()])
    queued = []
    for pid in patterns:
        case = AZ.analyze(pid, date)
        if not case.get("ok") or not case.get("draft"):
            continue
        draft = case["draft"]
        # supersede any previous draft for this pattern+period that is still pending
        T._execute(
            "DELETE FROM vrr_agent.action_queue WHERE id_pattern=%(p)s"
            " AND vrr_date=%(d)s AND stage='draft'",
            {"p": case["pattern_id"], "d": case["vrr_date"]})
        res = T.submit_for_approval(case["pattern_id"], case["vrr_date"],
                                    draft=draft, submitted_by="anomaly_to_queue")
        queued.append({"action_id": res["action_id"], "pattern": case["pattern_name"],
                       "vrr_date": case["vrr_date"], "severity": draft["severity"],
                       "action_type": draft["action_type"],
                       "owner_role": draft.get("owner_role"),
                       "verdict": draft.get("audit_verdict"),
                       "confidence": draft["confidence"]})
    return queued


if __name__ == "__main__":
    pat = sys.argv[1] if len(sys.argv) > 1 else None
    day = sys.argv[2] if len(sys.argv) > 2 else None
    for row in run(pat, day):
        print(f"  queued {row['action_id']}  {row['pattern']} {row['vrr_date']}"
              f"  [{row['severity']}] {row['action_type']} → {row['owner_role']}"
              f"  verdict={row['verdict']} ({row['confidence']})")
