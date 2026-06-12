"""Tests for grating_calib offset persistence + composition.

These cover the offset-based calibration model that replaced the broken
absolute-anchor reapply (which misaligned the grating on startup):

- ``compose_offset`` accumulates residual corrections.
- ``load_saved_offset`` reads the new ``calibration_offset_nm`` key and
  *ignores* legacy ``last_calibration_nm`` files (the regression guard).
"""

from __future__ import annotations

import json

import grating_calib


# ── compose_offset (pure) ──────────────────────────────────────────────


def test_compose_offset_from_none():
    # Peak seen at 531.7 should be 532.0 -> +0.3 residual, no prior offset.
    assert grating_calib.compose_offset(None, 531.7, 532.0) == 532.0 - 531.7


def test_compose_offset_accumulates():
    # Frame already shifted by +0.3; a fresh residual of +0.1 composes to +0.4.
    result = grating_calib.compose_offset(0.3, 531.9, 532.0)
    assert abs(result - 0.4) < 1e-9


# ── load_saved_offset ──────────────────────────────────────────────────


def test_load_saved_offset_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(grating_calib, "CALIB_FILE", tmp_path / "grating_calib.json")
    assert grating_calib.load_saved_offset() is None


def test_load_saved_offset_new_key(tmp_path, monkeypatch):
    f = tmp_path / "grating_calib.json"
    f.write_text(json.dumps({"calibration_offset_nm": 0.3}))
    monkeypatch.setattr(grating_calib, "CALIB_FILE", f)
    assert grating_calib.load_saved_offset() == 0.3


def test_load_saved_offset_ignores_legacy_absolute_anchor(tmp_path, monkeypatch):
    # The old format stored an absolute center anchor under a different key;
    # applying it as an offset is exactly the bug we fixed, so it must be
    # ignored rather than returned.
    f = tmp_path / "grating_calib.json"
    f.write_text(json.dumps({"last_calibration_nm": 532.3}))
    monkeypatch.setattr(grating_calib, "CALIB_FILE", f)
    assert grating_calib.load_saved_offset() is None


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    f = tmp_path / "grating_calib.json"
    monkeypatch.setattr(grating_calib, "CALIB_FILE", f)
    grating_calib.save_offset(-0.42)
    assert grating_calib.load_saved_offset() == -0.42
