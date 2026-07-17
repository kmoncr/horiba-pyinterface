"""Pure-logic tests for hwp_polarization: sequence math, records, CSV, scan loop.

No hardware, no QApplication — everything tested here is a module-level
function (or an injected-fake scan loop), so it runs in the default mock suite.
"""

from __future__ import annotations

import json

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
    assert seq["co"][0] == [2.0, 17.0, 47.0]  # OptoSigma: z_in + phi/2
    assert seq["co"][1] == [5.0, 20.0, 50.0]  # Thorlabs:  z_out + phi/2


def test_cross_shifts_only_outgoing_by_45():
    seq = compute_hwp_sequence(2.0, 5.0, [0.0, 30.0])
    assert seq["cross"][0] == seq["co"][0]  # incoming identical
    assert seq["cross"][1] == [50.0, 65.0]  # z_out + phi/2 + 45


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


import numpy as np
import pytest

from hwp_polarization import (
    load_records,
    make_record,
    parse_scan_csv,
    save_record,
)


# ── records persistence ──────────────────────────────────────────────


def _fake_record(stage: str, peak_angle_deg: float = 0.0) -> dict:
    """A record with every key load_records requires, so records-persistence
    tests exercise merge/dropping behavior without tripping the completeness
    check (that's covered separately by test_incomplete_record_is_dropped)."""
    return {
        "stage": stage,
        "peak_angle_deg": peak_angle_deg,
        "r2": 0.99,
        "visibility": 0.98,
        "timestamp": "2026-07-16T00:00:00",
        "csv_path": "x.csv",
    }


def test_record_roundtrip_both_roles(tmp_path):
    path = tmp_path / "hwp_calibration.json"
    save_record("incoming", _fake_record("optosigma", 2.1), path)
    save_record("outgoing", _fake_record("thorlabs", 5.0), path)
    recs = load_records(path)
    assert recs["incoming"]["peak_angle_deg"] == 2.1
    assert recs["outgoing"]["stage"] == "thorlabs"


def test_saving_one_role_preserves_the_other(tmp_path):
    path = tmp_path / "hwp_calibration.json"
    save_record("incoming", _fake_record("optosigma", 2.1), path)
    save_record("incoming", _fake_record("optosigma", 3.3), path)
    recs = load_records(path)
    assert recs["incoming"]["peak_angle_deg"] == 3.3  # overwritten
    save_record("outgoing", _fake_record("thorlabs", 5.0), path)
    assert load_records(path)["incoming"]["peak_angle_deg"] == 3.3  # preserved


def test_missing_file_gives_empty_records(tmp_path):
    assert load_records(tmp_path / "nope.json") == {}


def test_corrupt_file_gives_empty_records(tmp_path):
    path = tmp_path / "hwp_calibration.json"
    path.write_text("{not valid json")
    assert load_records(path) == {}


def test_unknown_keys_are_dropped(tmp_path):
    path = tmp_path / "hwp_calibration.json"
    path.write_text(
        json.dumps({"incoming": _fake_record("optosigma", 1.0), "junk": {"x": 1}})
    )
    recs = load_records(path)
    assert set(recs) == {"incoming"}


def test_incomplete_record_is_dropped(tmp_path):
    path = tmp_path / "hwp_calibration.json"
    path.write_text(
        json.dumps(
            {
                "incoming": {
                    "stage": "optosigma",
                    "peak_angle_deg": 2.1,
                    "r2": 0.99,
                    "visibility": 0.98,
                    "timestamp": "2026-07-16T14:32:01",
                    "csv_path": "hwp_calib_incoming_x.csv",
                },
                "outgoing": {"stage": "thorlabs"},  # missing everything else
            }
        )
    )
    recs = load_records(path)
    assert set(recs) == {"incoming"}


# ── make_record ──────────────────────────────────────────────────────

FAKE_FIT = {
    "peak_angle": 2.13,
    "r2": 0.9991,
    "visibility": 0.98,
    "amp2": 0.012,
    "amp4": 1.0,
}


def test_make_record_fields():
    rec = make_record("optosigma", FAKE_FIT, "hwp_calib_incoming_x.csv")
    assert rec["stage"] == "optosigma"
    assert rec["peak_angle_deg"] == pytest.approx(2.13)
    assert rec["r2"] == pytest.approx(0.9991)
    assert rec["visibility"] == pytest.approx(0.98)
    assert rec["amp2_over_amp4"] == pytest.approx(0.012)
    assert rec["csv_path"] == "hwp_calib_incoming_x.csv"
    assert "T" in rec["timestamp"]  # ISO datetime


def test_make_record_zero_amp4_yields_none_ratio():
    fit = dict(FAKE_FIT, amp4=0.0)
    assert make_record("optosigma", fit, "x.csv")["amp2_over_amp4"] is None


# ── parse_scan_csv ───────────────────────────────────────────────────


def test_parse_scan_csv_roundtrip(tmp_path):
    path = tmp_path / "scan.csv"
    path.write_text("angle_deg,power_W\n0.0,1.5e-3\n2.0,1.6e-3\n")
    ang, pw = parse_scan_csv(path)
    assert isinstance(ang, np.ndarray)
    assert list(ang) == [0.0, 2.0]
    assert pw[1] == pytest.approx(1.6e-3)


def test_parse_scan_csv_rejects_wrong_header(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("wavelength,counts\n500,10\n")
    with pytest.raises(ValueError, match="angle_deg"):
        parse_scan_csv(path)


# ── scan loop + worker ──────────────────────────────────────────────

from hwp_polarization import run_scan


class FakeStage:
    """Blocking degree get/set like the real controllers."""

    def __init__(self):
        self.moves: list[float] = []
        self._deg = 0.0

    @property
    def degree(self) -> float:
        return self._deg

    @degree.setter
    def degree(self, v: float):
        self._deg = v
        self.moves.append(v)


def test_run_scan_visits_all_angles_and_reports_points():
    stage = FakeStage()
    readings = iter([1.0, 2.0, 3.0])
    points = []
    angles, powers = run_scan(
        stage,
        lambda: next(readings),
        [0.0, 10.0, 20.0],
        0.0,
        lambda a, p: points.append((a, p)),
        lambda: False,
    )
    assert stage.moves == [0.0, 10.0, 20.0]
    assert powers == [1.0, 2.0, 3.0]
    assert points == list(zip(angles, powers))


def test_run_scan_stop_aborts_but_keeps_partial_data():
    stage = FakeStage()
    angles, powers = run_scan(
        stage,
        lambda: 1.0,
        [0.0, 10.0, 20.0, 30.0],
        0.0,
        lambda a, p: None,
        lambda: len(stage.moves) >= 2,
    )
    assert stage.moves == [0.0, 10.0]
    assert len(angles) == len(powers) == 2
