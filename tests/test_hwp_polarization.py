"""Pure-logic tests for hwp_polarization: sequence math, records, CSV, scan loop.

No hardware, no QApplication — everything tested here is a module-level
function (or an injected-fake scan loop), so it runs in the default mock suite.
"""

from __future__ import annotations

from hwp_polarization import (
    compute_hwp_sequence,
    format_angle_line,
    phi_values,
)


# ── phi_values ───────────────────────────────────────────────────────

def test_phi_values_inclusive_stop():
    assert phi_values(0, 180, 45) == [0, 45, 90, 135, 180]

def test_phi_values_nondivisible_step_stops_short():
    assert phi_values(0, 100, 45) == [0, 45, 90]

def test_phi_values_descending():
    assert phi_values(90, 0, 45) == [90, 45, 0]

def test_phi_values_bad_step_returns_empty():
    assert phi_values(0, 90, 0) == []
    assert phi_values(0, 90, -5) == []


# ── compute_hwp_sequence ─────────────────────────────────────────────

def test_co_stage_angles_are_zero_plus_half_phi():
    seq = compute_hwp_sequence(z_in=2.0, z_out=5.0, phis=[0.0, 30.0, 90.0])
    assert seq["co"][0] == [2.0, 17.0, 47.0]   # OptoSigma: z_in + phi/2
    assert seq["co"][1] == [5.0, 20.0, 50.0]   # Thorlabs:  z_out + phi/2

def test_cross_shifts_only_outgoing_by_45():
    seq = compute_hwp_sequence(2.0, 5.0, [0.0, 30.0])
    assert seq["cross"][0] == seq["co"][0]      # incoming identical
    assert seq["cross"][1] == [50.0, 65.0]      # z_out + phi/2 + 45

def test_lengths_match_phis():
    seq = compute_hwp_sequence(0.0, 0.0, [float(p) for p in range(0, 181, 15)])
    for opto, tl in seq.values():
        assert len(opto) == len(tl) == 13


# ── format_angle_line ────────────────────────────────────────────────

def test_format_angle_line_three_decimals():
    assert format_angle_line([0, 22.5, 47.125]) == "0.000, 22.500, 47.125"

def test_formatted_line_parses_like_sequencer_manual_box():
    # StageSequenceEditor.angles parses manual text by splitting on commas
    # and float()ing each token — assert the round trip survives that.
    line = format_angle_line([2.0, 17.0, 47.0])
    parsed = [float(tok) for tok in line.split(",") if tok.strip()]
    assert parsed == [2.0, 17.0, 47.0]
