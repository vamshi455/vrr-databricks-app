"""The gate must catch narration the decomposition does not support."""
from __future__ import annotations

from vrr_agent_open.core.decompose import decompose_vrr
from vrr_agent_open.core.faithfulness import (check_faithfulness, check_numbers,
                                              mentioned_terms)

A = {"oil_res": 1000.0, "water_res": 3000.0, "free_gas_res": 200.0,
     "water_inj_res": 4200.0, "gas_inj_res": 0.0}
B = {**A, "water_inj_res": 5400.0}          # injection-driven VRR rise
DEC = decompose_vrr(A, B)


def test_phrases_map_to_the_right_term():
    assert mentioned_terms("water injection rose") == {"water_inj_res"}
    assert mentioned_terms("produced water is up") == {"water_res"}
    assert mentioned_terms("nothing relevant here") == set()


def test_supported_driver_passes():
    r = check_faithfulness("VRR rose because water injection increased.", DEC)
    assert r["ok"] and r["supported"] == ["water_inj_res"]


def test_negligible_term_claimed_as_the_cause_is_rejected():
    r = check_faithfulness("The rise was driven by oil production.", DEC)
    assert not r["ok"] and r["violations"][0]["kind"] == "unsupported_driver"


def test_negligible_term_merely_listed_is_allowed():
    """Reporting a small term with its true share is honest, not a violation — only
    presenting it as the cause is."""
    r = check_faithfulness(
        "Water injection increased. Oil production contributed 0.2% of the move.", DEC)
    assert r["ok"], r["violations"]


def test_wrong_direction_is_rejected():
    r = check_faithfulness("Water injection decreased, pushing VRR up.", DEC)
    assert not r["ok"] and r["violations"][0]["kind"] == "wrong_direction"


def test_no_decomposition_means_nothing_to_contradict():
    assert check_faithfulness("anything at all", None)["ok"]


def test_uncited_numbers_are_caught():
    allowed = [1.327, 1.0, 0.9, 1.1]
    assert check_numbers("VRR is 1.327 against a 1.0 target.", allowed)["ok"]
    bad = check_numbers("VRR is about 1.4.", allowed)
    assert not bad["ok"] and bad["uncited"] == [1.4]


def test_negative_contributions_are_not_reported_as_uncited():
    """A production term that FELL prints as -0.0255 while the fact is -0.025477; the
    figure regex is deliberately unsigned, so comparison must be by magnitude."""
    assert check_numbers("water production contributed -0.0255 VRR", [-0.025477])["ok"]


def test_presentation_rounding_is_allowed():
    assert check_numbers("0.6% of the move", [0.56])["ok"]          # 1 dp of 0.56
    assert check_numbers("cumulative dVRR +0.36", [0.3552])["ok"]   # 2 dp of 0.3552


def test_invention_is_still_caught_after_the_rounding_allowance():
    bad = check_numbers("VRR is about 1.4.", [1.327, 1.0])
    assert not bad["ok"] and bad["uncited"] == [1.4]
    fabricated = check_numbers("oil averaged 908.39 STB/d", [6854.7, 227.02])
    assert not fabricated["ok"] and fabricated["uncited"] == [908.39]
