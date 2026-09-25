"""The architecture diagram, tested off-DB — and tested against the code it describes.

Two kinds of test here, and the second kind is the reason this file exists at all:

1. **Geometry and honesty.** Boxes do not overlap, every edge names a real box, and a
   missing fact produces no number rather than a wrong one.
2. **Anti-drift.** The diagram makes claims about the system — how many tools there are,
   what the approval stages are, that the files it points at exist. Each of those is
   asserted against the actual code, so renaming a module fails the suite instead of
   leaving a confident, wrong picture on screen. Same discipline as
   `test_help_topics.py`.
"""
from __future__ import annotations

import pathlib

import pytest

from vrr_agent_open.core import architecture as ARCH


# ------------------------------------------------------------------ shape ----
def test_every_edge_connects_two_real_boxes():
    ids = {n.id for n in ARCH.NODES}
    for a, b, _ in ARCH.EDGES:
        assert a in ids, f"edge from unknown node {a!r}"
        assert b in ids, f"edge to unknown node {b!r}"


def test_every_box_belongs_to_a_declared_band():
    bands = {b.id for b in ARCH.BANDS}
    for n in ARCH.NODES:
        assert n.band in bands, f"{n.id} sits in unknown band {n.band!r}"


def test_node_ids_are_unique():
    ids = [n.id for n in ARCH.NODES]
    assert len(ids) == len(set(ids))


def test_boxes_do_not_overlap():
    """A layout bug shows up as two boxes on top of each other, which is invisible in a
    unit test unless something checks the rectangles."""
    placed = ARCH.build({})["nodes"]
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            apart = (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                     or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])
            assert apart, f"{a['id']} overlaps {b['id']}"


def test_bands_stack_without_overlapping_and_fit_the_canvas():
    out = ARCH.build({})
    bands = out["bands"]
    for prev, nxt in zip(bands, bands[1:]):
        assert prev["y"] + prev["h"] <= nxt["y"], f"{prev['id']} overlaps {nxt['id']}"
    for n in out["nodes"]:
        assert n["x"] + n["w"] <= out["canvas"]["w"], f"{n['id']} runs off the canvas"
        assert n["y"] + n["h"] <= out["canvas"]["h"], f"{n['id']} runs off the canvas"


def test_edges_within_a_row_carry_no_label():
    """A caption on a same-row edge has GAP_X pixels to live in and is drawn over the
    boxes either side of it if it needs more. Found by screenshotting, not by reading —
    `on approval` was printed across the box it pointed at."""
    placed = {n["id"]: n for n in ARCH.build({})["nodes"]}
    for a, b, label in ARCH.EDGES:
        same_row = abs(placed[a]["y"] - placed[b]["y"]) < 4
        assert not (same_row and label), \
            f"edge {a}→{b} labels a {ARCH.GAP_X}px gap with {label!r}"


def test_no_caption_is_used_twice():
    """Two identical captions on one map read as two different facts."""
    labels = [lbl for _, _, lbl in ARCH.EDGES if lbl]
    assert len(labels) == len(set(labels)), f"duplicated edge captions in {labels}"


def test_every_box_sits_inside_its_own_band():
    out = ARCH.build({})
    band_box = {b["id"]: b for b in out["bands"]}
    for n in out["nodes"]:
        b = band_box[n["band"]]
        assert b["y"] <= n["y"] and n["y"] + n["h"] <= b["y"] + b["h"], \
            f"{n['id']} escapes the {n['band']} band"


# ---------------------------------------------------------------- honesty ----
def test_a_missing_fact_renders_no_number():
    """The whole point. An unmeasured box must be blank, never zero and never a dash —
    "no cards in this lane" and "I could not read the queue" are different claims."""
    for n in ARCH.build({})["nodes"]:
        spec = next(s for s in ARCH.NODES if s.id == n["id"])
        if spec.keys and not spec.static:
            assert n["value"] is None, f"{n['id']} invented {n['value']!r} from no facts"


def test_a_partially_measured_box_renders_no_number():
    """`corpora` needs two counts. One of them alone must not be formatted into a string
    that reads like a measurement of both."""
    out = ARCH.build({"docs_reservoir": 4})
    corpora = next(n for n in out["nodes"] if n["id"] == "corpora")
    assert corpora["value"] is None


def test_facts_produce_the_expected_text():
    out = ARCH.build({"docs_reservoir": 4, "docs_help": 6, "retrieval_floor": 0.62,
                      "raw_rows": 272880})
    values = {n["id"]: n["value"] for n in out["nodes"]}
    assert values["corpora"] == "4 reservoir · 6 help"
    assert values["floor"] == "abstain below 0.62"
    assert values["raw"] == "272,880 daily volume rows"


def test_a_wrongly_typed_fact_is_dropped_not_crashed():
    """A probe that returns a string where a count was expected must blank the box, not
    500 the endpoint that draws the whole map."""
    out = ARCH.build({"raw_rows": "lots"})
    raw = next(n for n in out["nodes"] if n["id"] == "raw")
    assert raw["value"] is None


def test_static_boxes_state_a_rule_not_a_measurement():
    """`core.physics` shows a formula, which is true whether or not the database is up."""
    physics = next(n for n in ARCH.build({})["nodes"] if n["id"] == "physics")
    assert physics["value"] == "FACTOR · VOLUME · FVF"


def test_no_box_leaks_a_host_or_connection_string():
    """This endpoint is served under the share-mode read guard but has no redaction step
    of its own, so nothing it can emit may be an address."""
    blob = " ".join(f"{n.label} {n.what} {n.guardrail} {n.static}" for n in ARCH.NODES)
    # `password=` and not `password`: "OAuth2 password grant" is the name of a protocol,
    # and a check that cannot tell a protocol from a credential gets deleted the first
    # time it cries wolf.
    for leak in ("://", "localhost", "127.0.0.1", "password=", "secret=", ":5432"):
        assert leak not in blob, f"architecture copy contains {leak!r}"


# -------------------------------------------------------------- anti-drift ----
def test_the_files_each_box_points_at_actually_exist():
    """Click a box and it tells you where to look. If that path is stale the box is
    lying, and a confidently wrong pointer costs more than no pointer."""
    roots = [pathlib.Path("src/vrr_agent_open"), pathlib.Path(".")]
    for n in ARCH.NODES:
        for rel in n.files:
            assert any((root / rel).exists() for root in roots), \
                f"{n.id} points at {rel}, which does not exist"


def test_the_approval_band_matches_the_real_chain():
    from vrr_agent_open.core.approval import STAGES

    drawn = [n.id for n in ARCH.NODES if n.band == "approval"]
    assert drawn == list(STAGES), f"board drawn as {drawn}, chain is {list(STAGES)}"


def test_executed_box_points_at_the_writeback_job():
    """The diagram used to say the ρ loop was not built. If the job moves, this box
    must move with it rather than keep advertising a closed loop that is not there."""
    spec = next(n for n in ARCH.NODES if n.id == "executed")
    assert "pipeline/outcome_writeback.py" in spec.files
    assert "not built" not in spec.what.lower()


def test_every_approval_stage_has_a_count_key():
    for stage in ("draft", "analyst", "rm", "site", "executed"):
        spec = next(n for n in ARCH.NODES if n.id == stage)
        assert spec.keys == (f"queue_{stage}",)


def test_the_route_supplies_every_fact_the_diagram_can_display():
    """The contract between the two halves. A box added here without a probe added there
    would silently render blank forever, which looks identical to a database outage."""
    from vrr_agent_open.api.routes_architecture import STAGE_KEYS

    src = pathlib.Path("src/vrr_agent_open/api/routes_architecture.py").read_text()
    for key in ARCH.fact_keys():
        # The lane counts are generated from the approval chain rather than typed out,
        # so they are checked against that tuple instead of against the source text.
        covered = f'"{key}"' in src or key in STAGE_KEYS
        assert covered, f"no probe in the route fills {key!r}"


@pytest.mark.parametrize("node_id", [n.id for n in ARCH.NODES])
def test_every_box_explains_itself(node_id):
    spec = next(n for n in ARCH.NODES if n.id == node_id)
    assert spec.what.strip(), f"{node_id} has no explanation"
    assert spec.what.strip().endswith("."), f"{node_id}'s explanation is not a sentence"
