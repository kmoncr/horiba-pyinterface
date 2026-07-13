"""Tests for the dual-sequence per-step naming helpers.

These are pure functions in horibagui (no widgets constructed), so they
run in the default mock suite without pytest-qt.
"""

from __future__ import annotations

from horibagui import parse_step_names, step_base_name


# ── parse_step_names ─────────────────────────────────────────────────

def test_parse_empty_text_gives_no_names():
    assert parse_step_names("") == []
    assert parse_step_names("   ") == []
    assert parse_step_names(" , , ") == []


def test_parse_single_name():
    assert parse_step_names("sampleA") == ["sampleA"]


def test_parse_multiple_names_strips_whitespace():
    assert parse_step_names(" LR , RL ") == ["LR", "RL"]


# ── step_base_name ───────────────────────────────────────────────────

def test_no_names_falls_back_to_file_input_name():
    assert step_base_name([], 1, "DATA") == "DATA"
    assert step_base_name([], 5, "DATA") == "DATA"


def test_single_name_keeps_prefix_step_numbering():
    # Original "name prefix" behaviour: one name → name_1, name_2, …
    assert step_base_name(["sampleA"], 1, "DATA") == "sampleA_1"
    assert step_base_name(["sampleA"], 3, "DATA") == "sampleA_3"


def test_multiple_names_cycle_across_steps():
    # Entry names carry through repeats: 1,2,1,2,1,2 → LR,RL,LR,RL,LR,RL
    names = ["LR", "RL"]
    got = [step_base_name(names, i, "DATA") for i in range(1, 7)]
    assert got == ["LR", "RL", "LR", "RL", "LR", "RL"]


def test_three_names_cycle():
    names = ["a", "b", "c"]
    got = [step_base_name(names, i, "DATA") for i in range(1, 8)]
    assert got == ["a", "b", "c", "a", "b", "c", "a"]
