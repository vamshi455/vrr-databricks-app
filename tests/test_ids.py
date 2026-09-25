"""Surrogate keys must be well-formed and reproducible — a reseed can't renumber the field."""
from __future__ import annotations

from vrr_agent_open.core.ids import ID_CHARS, hex_id, is_valid, short


def test_format_matches_the_database_check():
    for parts in (("pattern", "UNITY"), ("completion", "UNITY", "P", 1), ("x",)):
        value = hex_id(*parts)
        assert len(value) == ID_CHARS
        assert is_valid(value)                       # ^[0-9A-F]{16}$


def test_deterministic_and_distinct():
    assert hex_id("pattern", "UNITY") == hex_id("pattern", "UNITY")
    assert hex_id("pattern", "UNITY") != hex_id("pattern", "HORIZON")
    assert hex_id("pattern", "UNITY") != hex_id("completion", "UNITY")   # namespaced
    assert hex_id("a", "b") != hex_id("ab")                             # separator matters


def test_salt_changes_the_namespace():
    assert hex_id("pattern", "UNITY", salt="other") != hex_id("pattern", "UNITY")


def test_rejects_malformed_values():
    assert not is_valid("PAT-001")
    assert not is_valid(hex_id("x").lower())         # lowercase hex is not our form
    assert not is_valid(hex_id("x")[:15])
    assert not is_valid(None)


def test_short_is_display_only():
    value = hex_id("pattern", "UNITY")
    assert short(value).startswith(value[:6]) and len(short(value)) == 7
    assert short("ABC") == "ABC"                     # nothing to shorten
