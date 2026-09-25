"""The pattern schematic is a figure people will trust, so its placement rule is tested
like any other computation in `core/` — off-DB, deterministic, no stack needed.

What matters here is not that the picture is pretty but that it cannot lie: the shape it
claims must follow from the well counts, distance must fall as contribution rises, and a
completion shared with another pattern must be flagged every time.
"""
from __future__ import annotations

import math

from vrr_agent_open.core import pattern_layout as PL


def comp(cid, role, factor=1.0, share=0.25, **kw):
    key = "share_of_injection" if role == "injector" else "share_of_production"
    return {"completion_id": cid, "completion_name": cid, "role": role,
            "factor": factor, key: share, "pvt_methods": "exact", **kw}


def five_spot():
    return [comp("I1", "injector", share=1.0)] + [
        comp(f"P{i}", "producer", share=0.25) for i in range(1, 5)
    ]


# ---- naming the shape -------------------------------------------------------

def test_one_injector_four_producers_is_a_five_spot():
    out = PL.build(five_spot())
    assert out["geometry"] == "five_spot"
    assert out["geometry_label"] == "Five-spot"


def test_canonical_counts_get_their_textbook_names():
    assert PL.classify(1, 2) == "line_drive"
    assert PL.classify(1, 6) == "seven_spot"
    assert PL.classify(1, 8) == "nine_spot"


def test_uncanonical_counts_are_called_irregular_not_rounded_to_the_nearest_shape():
    # Five producers is NOT a five-spot; claiming so would put the well count and the
    # label in disagreement on screen.
    assert PL.classify(1, 5) == "irregular"


def test_more_than_one_injector_and_none_at_all_are_their_own_cases():
    assert PL.classify(2, 4) == "multi_injector"
    assert PL.classify(0, 3) == "no_injector"


# ---- placement --------------------------------------------------------------

def test_the_lone_injector_sits_at_the_centre():
    inj = next(n for n in PL.build(five_spot())["nodes"] if n["role"] == "injector")
    assert (inj["x"], inj["y"]) == (0.0, 0.0)


def test_five_spot_producers_land_on_the_square_corners():
    prods = [n for n in PL.build(five_spot())["nodes"] if n["role"] == "producer"]
    # Corners of a square: |x| == |y| for every one of them.
    for p in prods:
        assert math.isclose(abs(p["x"]), abs(p["y"]), rel_tol=1e-6)
    # ...and all four are distinct quadrants, i.e. it is a square not a stack.
    assert len({(p["x"] > 0, p["y"] > 0) for p in prods}) == 4


def test_a_stronger_contributor_is_drawn_closer_to_the_injector():
    wells = [comp("I1", "injector", share=1.0),
             comp("P_near", "producer", factor=0.95, share=0.5),
             comp("P_far", "producer", factor=0.15, share=0.5)]
    by = {n["completion_name"]: n for n in PL.build(wells)["nodes"]}
    def dist(n):
        return math.hypot(n["x"], n["y"])
    assert dist(by["P_near"]) < dist(by["P_far"])


def test_layout_is_deterministic_and_independent_of_input_order():
    wells = five_spot()
    a = PL.build(wells)
    b = PL.build(list(reversed(wells)))
    assert a["nodes"] == b["nodes"]


def test_the_biggest_producer_is_placed_first_so_the_figure_is_stable():
    wells = [comp("I1", "injector", share=1.0),
             comp("P_small", "producer", share=0.2),
             comp("P_big", "producer", share=0.8)]
    prods = [n for n in PL.build(wells)["nodes"] if n["role"] == "producer"]
    assert [p["completion_name"] for p in prods] == ["P_big", "P_small"]


# ---- multiple injectors -----------------------------------------------------

def test_injectors_do_not_sit_on_top_of_each_other():
    """Three injectors on a fixed tiny ring overlapped into one blob with their captions
    mashed together. The ring has to grow with the count."""
    wells = [comp(f"I{i}", "injector", share=0.33) for i in range(3)] + \
            [comp(f"P{i}", "producer", share=0.25) for i in range(4)]
    inj = [n for n in PL.build(wells)["nodes"] if n["role"] == "injector"]
    gaps = [math.dist((a["x"], a["y"]), (b["x"], b["y"]))
            for i, a in enumerate(inj) for b in inj[i + 1:]]
    # Node radii top out at 20 units, so anything under 40 apart is visibly overlapping.
    assert min(gaps) > 40


def test_producers_clear_the_whole_injector_ring():
    wells = [comp(f"I{i}", "injector", share=0.33) for i in range(3)] + \
            [comp("P1", "producer", factor=1.0, share=1.0)]
    out = PL.build(wells)
    hub = out["hub"]["radius"]
    prod = next(n for n in out["nodes"] if n["role"] == "producer")
    assert math.hypot(prod["x"], prod["y"]) > hub + 40


def test_one_sweep_line_per_producer_regardless_of_injector_count():
    """Not one per injector-producer PAIR. Three injectors and eight producers would be
    24 crossing lines, and — the real reason — the allocation data does not say which
    injector feeds which producer, so 24 lines would assert 24 relationships nothing in
    the database supports."""
    wells = [comp(f"I{i}", "injector", share=0.33) for i in range(3)] + \
            [comp(f"P{i}", "producer", share=0.125) for i in range(8)]
    out = PL.build(wells)
    assert len(out["links"]) == 8
    assert out["hub"]["radius"] > 0


def _ring(name_len, n=3):
    wells = [comp("X" * name_len + str(i), "injector", share=1.0 / n) for i in range(n)]
    return PL.build(wells + [comp("P1", "producer")])["hub"]["radius"]


def test_the_injector_ring_follows_whichever_caption_line_is_wider():
    """A count-only ring was right for ALIOTH-I1 and too tight for ARCTURUS-I3 at the
    same count. But the name is not always the wide line: under every well sits
    "f 0.52 · 28.0%", a fixed 14 glyphs, and that is what actually collided on ALIOTH.
    So the ring is spaced off the wider of the two, and a very long name pushes past it."""
    assert _ring(20) > _ring(6)                    # name wins once it exceeds the value line
    assert _ring(6) == _ring(9)                    # below that, the value line is the floor


def test_the_ring_clears_the_value_line_even_for_short_names():
    # ALIOTH-I1 / ALIOTH-I2 cleared by name and touched by caption. Chord for three on
    # a ring is 2·R·sin(60°), and it has to beat the value line's own width.
    r = _ring(9)
    assert 2 * r * math.sin(math.pi / 3) > PL.SUB_CHARS * PL.SUB_CHAR_W


def test_a_single_injector_leaves_the_hub_at_the_centre():
    out = PL.build(five_spot())
    assert out["hub"] == {"x": 0.0, "y": 0.0, "radius": 0.0}


# ---- the flags that change how the figure is read ---------------------------

def test_a_completion_in_two_patterns_is_flagged_as_shared():
    wells = five_spot()
    wells[1]["n_patterns"] = 3
    out = PL.build(wells)
    assert out["shared"] == ["P1"]
    assert next(n for n in out["nodes"] if n["completion_name"] == "P1")["shared"] is True


def test_extrapolated_pvt_marks_the_well_low_confidence():
    wells = five_spot()
    wells[2]["pvt_methods"] = "exact,extrapolated"
    assert PL.build(wells)["low_confidence"] == ["P2"]


def test_idle_completions_are_kept_but_carry_no_sweep_line():
    wells = five_spot() + [comp("P_shut", "idle", share=0.0)]
    out = PL.build(wells)
    assert out["n_idle"] == 1
    assert "P_shut" not in {l["to"] for l in out["links"]}
    assert len(out["links"]) == 4                      # one injector × four producers


def test_every_payload_declares_itself_a_schematic():
    # The API contract the view leans on to print its "not a map" caption. If this ever
    # goes false, something is claiming to know where the wells are.
    assert PL.build(five_spot())["is_schematic"] is True


def test_the_caption_agrees_with_the_well_counts_it_describes():
    out = PL.build(five_spot())
    assert "1 injector, 4 producers" in out["caption"]
    assert out["geometry_label"] in out["caption"]


# ---- degenerate input should still draw -------------------------------------

def test_no_completions_yields_an_empty_but_valid_figure():
    out = PL.build([])
    assert out["geometry"] == "no_injector"
    assert out["nodes"] == [] and out["links"] == []


def test_missing_factor_and_shares_fall_back_instead_of_raising():
    out = PL.build([{"completion_id": "I1", "role": "injector"},
                    {"completion_id": "P1", "role": "producer"}])
    assert out["n_producers"] == 1
    assert all(math.isfinite(n["x"]) and math.isfinite(n["y"]) for n in out["nodes"])
