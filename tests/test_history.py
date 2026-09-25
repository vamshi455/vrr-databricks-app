"""Unit tests for the chat-transcript helpers — off-DB.

`log_turn` reads a `chat.respond()` result whose `meta` keys are branch-dependent and
every one of them optional, so the tests that matter here are about surviving a result
dict with nothing in it, and about not writing a 200 KB payload into every row.
"""
import json

from vrr_agent_open.agent import history as H


class _Cursor:
    """Records the params a would-be INSERT was called with."""
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, sink):
        self.sink = sink

    def cursor(self, *a, **k):
        return _Cursor(self.sink)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch):
    sink = []
    monkeypatch.setattr(H, "_connect", lambda **k: _Conn(sink))
    return sink


def test_chat_id_shape(monkeypatch):
    _capture(monkeypatch)
    chat_id = H.log_turn(pattern_id="P", pattern_name="UNITY", date="2026-04-01",
                         question="why?", result={"text": "because"}, asked_by="me")
    assert chat_id.startswith("CHT-") and len(chat_id) == 14


def test_survives_a_result_with_no_meta_at_all(monkeypatch):
    """Every deterministic branch of respond() omits most meta keys; none may KeyError."""
    sink = _capture(monkeypatch)
    H.log_turn(pattern_id="P", pattern_name=None, date=None, question="q",
               result={"text": "a"}, asked_by=None)
    params = sink[0][1]
    assert params["i"] is None and params["model"] is None and params["gate"] is None
    assert params["llm"] is False and params["tools"] is None and params["meta"] is None


def test_promotes_meta_fields_to_columns(monkeypatch):
    sink = _capture(monkeypatch)
    result = {"text": "a", "intent": "explain",
              "meta": {"llm": True, "model": "qwen2.5:7b", "gate": "passed",
                       "tools_called": ["VRR_AUDIT", "VRR_DECOMPOSE"]}}
    H.log_turn(pattern_id="P", pattern_name="UNITY", date="2026-04-01", question="q",
               result=result, asked_by="analyst.demo", agentic=True)
    params = sink[0][1]
    assert params["i"] == "explain" and params["llm"] is True
    assert params["model"] == "qwen2.5:7b" and params["gate"] == "passed"
    assert json.loads(params["tools"]) == ["VRR_AUDIT", "VRR_DECOMPOSE"]
    assert params["ag"] is True
    assert json.loads(params["meta"])["gate"] == "passed"      # whole dict kept too


def test_oversized_payload_is_summarised_not_stored(monkeypatch):
    """VRR_TREND/VRR_LINEAGE return whole series — a row must not carry 200 KB of them."""
    sink = _capture(monkeypatch)
    huge = {"rows": [{"vrr": i, "pad": "x" * 200} for i in range(2000)], "ok": True}
    H.log_turn(pattern_id="P", pattern_name="UNITY", date=None, question="q",
               result={"text": "a", "data": huge}, asked_by=None)
    stored = json.loads(sink[0][1]["payload"])
    assert stored["truncated"] is True
    assert stored["keys"] == ["ok", "rows"]
    assert len(sink[0][1]["payload"]) < H.MAX_PAYLOAD_CHARS


def test_small_payload_is_stored_verbatim(monkeypatch):
    sink = _capture(monkeypatch)
    H.log_turn(pattern_id="P", pattern_name="U", date=None, question="q",
               result={"text": "a", "data": {"vrr": 1.234}}, asked_by=None)
    assert json.loads(sink[0][1]["payload"]) == {"vrr": 1.234}


def test_empty_payload_is_null(monkeypatch):
    sink = _capture(monkeypatch)
    H.log_turn(pattern_id="P", pattern_name="U", date=None, question="q",
               result={"text": "a", "data": {}}, asked_by=None)
    assert sink[0][1]["payload"] is None
