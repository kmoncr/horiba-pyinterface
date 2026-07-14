"""Pure-logic tests for the dual-stage sequence queue plan.

`build_dual_sequence_queue` decides the order scans are queued in and the
base filename each one gets: the WHOLE sequence runs once per repeat (the
repeat is the outer loop), and every step keeps its own name on each pass.
No widgets constructed, so these run in the default mock suite without
pytest-qt.
"""

from __future__ import annotations

from horibagui import build_dual_sequence_queue


STEPS = [(0.0, 0.0, "RL"), (45.0, 0.0, "LR")]


# ── repeat ordering ──────────────────────────────────────────────────

def test_single_repeat_runs_each_step_once():
    plan = build_dual_sequence_queue(STEPS, 1, "DATA")
    assert plan == [("RL", 0.0, 0.0, 1), ("LR", 45.0, 0.0, 1)]


def test_repeats_rerun_whole_sequence_not_each_step():
    plan = build_dual_sequence_queue(STEPS, 3, "DATA")
    # RL, LR, RL, LR, RL, LR — never RL, RL, RL, LR, LR, LR.
    assert [p[0] for p in plan] == ["RL", "LR", "RL", "LR", "RL", "LR"]
    # The repeat index (the S# in filenames) increments per full pass.
    assert [p[3] for p in plan] == [1, 1, 2, 2, 3, 3]


def test_names_stay_bound_to_their_step_across_repeats():
    plan = build_dual_sequence_queue(STEPS, 2, "DATA")
    for base, opto, tl, _rep in plan:
        if base == "RL":
            assert (opto, tl) == (0.0, 0.0)
        else:
            assert (opto, tl) == (45.0, 0.0)


# ── name fallback ────────────────────────────────────────────────────

def test_blank_name_falls_back_to_global_filename():
    steps = [(0.0, 0.0, ""), (45.0, 0.0, "   "), (90.0, 0.0, "LR")]
    plan = build_dual_sequence_queue(steps, 1, "DATA")
    assert [p[0] for p in plan] == ["DATA", "DATA", "LR"]


def test_names_are_stripped():
    plan = build_dual_sequence_queue([(0.0, 0.0, " RL ")], 1, "DATA")
    assert plan[0][0] == "RL"


def test_empty_steps_give_empty_plan():
    assert build_dual_sequence_queue([], 5, "DATA") == []
