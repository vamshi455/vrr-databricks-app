"""Deterministic scorers must read the span tree correctly — they are the hard signal.

Traces are faked with the minimum shape the scorers touch, so these run off-DB and
off-MLflow like the rest of the suite.
"""
from __future__ import annotations

from types import SimpleNamespace

from vrr_agent_open.evaluation import custom_scorers as CS


def _span(name, span_type, inputs=None, outputs=None, parent_id="p", start=0):
    return SimpleNamespace(name=name, span_type=span_type, inputs=inputs or {},
                           outputs=outputs, parent_id=parent_id, start_time_ns=start)


def _trace(spans, duration=100):
    return SimpleNamespace(data=SimpleNamespace(spans=spans),
                           info=SimpleNamespace(execution_duration=duration, tags={}))


ROOT = _span("chat.respond", "AGENT", outputs={"text": "VRR is 1.327 on target."},
             parent_id=None)


def test_tool_sequence_is_start_ordered():
    t = _trace([ROOT,
                _span("RECOMMEND_CHANGE", "TOOL", start=20),
                _span("VRR_AUDIT", "TOOL", start=10)])
    assert CS._tool_sequence(t) == ["VRR_AUDIT", "RECOMMEND_CHANGE"]
    assert CS.tools_used(trace=t) == 2


def test_audit_before_advice_catches_the_wrong_order():
    good = _trace([ROOT, _span("VRR_AUDIT", "TOOL", start=1),
                   _span("RECOMMEND_CHANGE", "TOOL", start=2)])
    bad = _trace([ROOT, _span("RECOMMEND_CHANGE", "TOOL", start=1),
                  _span("VRR_AUDIT", "TOOL", start=2)])
    missing = _trace([ROOT, _span("RECOMMEND_CHANGE", "TOOL", start=1)])
    no_advice = _trace([ROOT, _span("VRR_GET", "TOOL", start=1)])
    assert CS.audit_before_advice(trace=good)
    assert not CS.audit_before_advice(trace=bad)
    assert not CS.audit_before_advice(trace=missing)
    assert CS.audit_before_advice(trace=no_advice)        # nothing to gate


def test_numbers_grounded_reads_tool_spans():
    grounded = _trace([ROOT, _span("VRR_GET", "TOOL", outputs={"vrr": 1.3274})])
    invented = _trace([_span("chat.respond", "AGENT", parent_id=None,
                             outputs={"text": "VRR is about 1.9."}),
                       _span("VRR_GET", "TOOL", outputs={"vrr": 1.3274})])
    assert CS.numbers_grounded(trace=grounded)            # 1.327 rounds from 1.3274
    assert not CS.numbers_grounded(trace=invented)


def test_no_advice_on_artifact():
    artifact_ok = _trace([_span("chat.respond", "AGENT", parent_id=None,
                                outputs={"text": "Inputs are suspect; investigate."}),
                          _span("VRR_AUDIT", "TOOL", outputs={"verdict": "DATA_ARTIFACT"})])
    artifact_bad = _trace([_span("chat.respond", "AGENT", parent_id=None,
                                 outputs={"text": "Reduce injection by 15%."}),
                           _span("VRR_AUDIT", "TOOL", outputs={"verdict": "DATA_ARTIFACT"})])
    assert CS.no_advice_on_artifact(trace=artifact_ok)
    assert not CS.no_advice_on_artifact(trace=artifact_bad)


def test_gate_rejection_is_detected():
    rejected = _trace([ROOT, _span("faithfulness_gate", "CHAIN",
                                   outputs={"gate": "REJECTED", "violations": ["x"]})])
    assert not CS.gate_passed(trace=rejected)
    assert CS.gate_passed(trace=_trace([ROOT]))


def test_latency_is_reported_from_trace_info():
    assert CS.latency_ms(trace=_trace([ROOT], duration=8123)) == 8123.0
