"""The app-help path: what it answers, what it refuses to answer, and staying true.

Two different jobs here, and the second is the one that matters in six months:

1. **Routing.** An app question must reach `help`, and a reservoir question must NOT.
   The dangerous direction is the second one — replacing a computed answer about the
   physics with a page of prose about a screen is a downgrade the reader cannot detect.
2. **Truth.** `core/help_topics.py` makes claims about views, roles and stages. Those are
   asserted against the code that actually implements them, so re-wiring the approval
   chain or renaming a view fails here rather than silently leaving the agent confidently
   describing an app that no longer exists.
"""
from __future__ import annotations

import pytest

from vrr_agent_open.agent import chat as CH
from vrr_agent_open.core import help_topics as HELP


# ------------------------------------------------------------------ routing ----
@pytest.mark.parametrize("q,topic", [
    ("How do I use this app?", "overview"),
    ("what can you do?", "overview"),
    ("How do I move a card from the analyst lane to RM?", "move_card"),
    # The literal question that exposed the gap: "zone" and "card" were not app nouns,
    # so this routed to `explain` and answered with a list of patterns. People say
    # "zone"/"column"/"card" because that is the vocabulary of every other board tool.
    ("How do I move a card from the analyst zone to the RM zone?", "move_card"),
    ("how do I move it from the analyst column to rm?", "move_card"),
    ("why can't I drag this card?", "move_card"),
    ("what does the approval board show?", "approvals"),
    ("how do I upload a document?", "knowledge"),
    ("my file was rejected on upload, why?", "knowledge"),
    ("what is the portfolio view for?", "portfolio"),
    ("what does the report screen show me?", "report"),
    ("how do I sign in?", "signin"),
    ("which role can approve, and where do I see mine?", "roles"),
    ("what does the amber suspect flag on screen mean?", "suspect"),
    ("how do I use the chat drawer?", "chat"),
])
def test_app_questions_reach_the_right_topic(q, topic):
    assert HELP.is_help_question(q), f"{q!r} was not recognised as an app question"
    assert HELP.answer(q)["topic"] == topic


@pytest.mark.parametrize("q", [
    "How is VRR calculated?",
    "Why is UNITY's VRR high in April 2026?",
    "What is a good VRR?",
    "Which patterns are furthest from target?",
    "What do the documents say about changing injection rates?",
    "Is the April number actually correct?",
    "recommend a valve change for MERIDIAN",
    "list the completions in this pattern",
])
def test_reservoir_questions_are_never_hijacked(q):
    """The costly direction. 'How is VRR calculated' must reach `lineage` and get the
    real derivation, not a paragraph about the Lineage *screen*."""
    assert not HELP.is_help_question(q)
    assert CH.detect_intent(q) != "help"


@pytest.mark.parametrize("q,intent", [
    ("How is VRR calculated?", "lineage"),
    ("Which patterns are furthest from target?", "portfolio"),
    ("What do the documents say about injection limits?", "knowledge"),
    ("Is this number correct?", "audit"),
])
def test_existing_intents_still_route_as_before(q, intent):
    """Adding `help` in front of the keyword table must not shift anything behind it."""
    assert CH.detect_intent(q) == intent


def test_an_app_noun_alone_is_not_enough():
    """`is_help_question` needs a noun AND a matching topic, so a stray 'screen' in a
    reservoir question cannot drag it into the help path."""
    assert not HELP.is_help_question("is the pressure on screen sane for this reservoir?")


def test_longer_keyword_wins_over_a_generic_one():
    """'analyst to rm' is specific; 'approve' is not. The specific topic must win."""
    assert HELP.answer("how do I move a card from analyst to rm?")["topic"] == "move_card"


# -------------------------------------------------------------------- answers ----
def test_the_answer_is_returned_verbatim_with_no_model():
    out = CH._help_answer("how do I use this app?")
    assert out["intent"] == "help"
    assert out["meta"]["llm"] is False
    assert HELP.BY_ID["overview"].body.split("\n")[0] in out["text"]
    assert out["data"]["source"] == "core/help_topics.py"


def test_an_unmatched_app_question_lists_what_it_can_answer(monkeypatch):
    """No topic and no ingested guide must produce a menu, never a guess."""
    monkeypatch.setattr(CH.T, "search_knowledge",
                        lambda *a, **k: {"ok": True, "hits": []})
    out = CH._help_answer("how do I export this to SAP?")
    assert out["meta"]["llm"] is False
    assert "I don't have a written answer" in out["text"]
    assert "The Approvals board" in out["text"]          # the menu of real topics


def test_the_long_tail_searches_only_the_app_corpus(monkeypatch):
    """The whole point of doc_kind: a question about a button must not be answered out
    of the injection-change procedure."""
    seen = {}
    monkeypatch.setattr(CH.T, "search_knowledge",
                        lambda q, k=3, doc_kind=None: seen.update(kind=doc_kind) or
                        {"ok": True, "hits": []})
    CH._help_answer("how do I export this to SAP?")
    assert seen["kind"] == "app_help"


# ---------------------------------------------------------------------- truth ----
def test_view_names_match_the_actual_nav():
    """If a view is renamed in App.tsx, this fails rather than the agent describing a
    screen that no longer exists."""
    import pathlib
    import re

    src = pathlib.Path("web/src/App.tsx").read_text()
    labels = set(re.findall(r'label:\s*"([^"]+)"', src))
    assert set(HELP.VIEWS) <= labels, f"help lists views the nav does not: {set(HELP.VIEWS) - labels}"
    for t in HELP.TOPICS:
        assert t.view in {*HELP.VIEWS, ""}, f"topic {t.id} names an unknown view {t.view!r}"


def test_the_approval_chain_described_matches_the_server():
    """`approvals` states who advances what. That is enforced in one dict server-side;
    if the chain is re-wired, the written answer has to be re-written with it."""
    from vrr_agent_open.api.routes_approvals import APPROVER_FOR_STAGE

    body = HELP.BY_ID["approvals"].body
    for stage, role in APPROVER_FOR_STAGE.items():
        assert f"`{stage}`" in body, f"stage {stage!r} is not described"
        assert f"**{role}**" in body, f"approver {role!r} is not named"


def test_roles_described_are_roles_the_database_accepts():
    import pathlib
    import re

    ddl = pathlib.Path("src/vrr_agent_open/api/auth.py").read_text()
    allowed = set(re.search(r"role IN \(([^)]+)\)", ddl).group(1).replace("'", "").split(","))
    body = HELP.BY_ID["roles"].body
    for role in allowed:
        assert f"`{role.strip()}`" in body, f"role {role!r} is not documented"


def test_accepted_upload_types_match_the_validator():
    """The Knowledge answer quotes extensions and limits; those come from one module."""
    from vrr_agent_open.core import upload_validation as UV

    body = HELP.BY_ID["knowledge"].body
    for ext in UV.ALLOWED_SUFFIXES:
        assert f"`{ext}`" in body, f"{ext} is accepted but undocumented"


def test_every_topic_is_reachable_and_well_formed():
    assert len({t.id for t in HELP.TOPICS}) == len(HELP.TOPICS)
    for t in HELP.TOPICS:
        assert t.keywords and t.body.strip() and t.title
        for ref in t.see_also:
            assert ref in HELP.BY_ID, f"{t.id} points at missing topic {ref!r}"
        assert HELP.match(t.keywords[0]), f"{t.id}'s first keyword matches nothing"
