#!/usr/bin/env python
"""HWP polarization calibration & dual-sequence angle-list generator.

GUI workflow (see docs/superpowers/specs/2026-07-16-hwp-polarization-design.md):
calibrate the incoming HWP (OptoSigma stage) and the outgoing HWP (Thorlabs
K10CR2) one at a time against a fixed polarizer + PM100A, persist each fit
to hwp_calibration.json, then generate co- and cross-polarized stage-angle
lists ready to paste into the Dual-Stage Synchronized Sequence manual boxes.

Physics: fit_waveplate locates each HWP's fast-axis stage angle z (peak,
mod 90 deg). A HWP at angle beta maps polarization p -> 2*beta - p, so to
rotate the incident polarization by phi and analyze it back onto the fixed
analyzer: incoming stage = z_in + phi/2; outgoing stage = z_out + phi/2
(co-polarized) or z_out + phi/2 + 45 deg (cross-polarized).

Run with:  uv run python hwp_polarization.py
"""

from __future__ import annotations

import csv
import json
import pathlib
from datetime import datetime

import numpy as np
from loguru import logger


# ── pure sequence math ──────────────────────────────────────────────


def phi_values(start: float, stop: float, step: float) -> list[float]:
    """Inclusive-stop phi grid, same semantics as the sequencer's sweep
    editor: empty on step <= 0, descending allowed, 1e-9 end tolerance."""
    if step <= 0:
        return []
    out = []
    v = start
    if stop >= start:
        while v <= stop + 1e-9:
            out.append(round(v, 6))
            v += step
    else:
        step = -abs(step)
        while v >= stop - 1e-9:
            out.append(round(v, 6))
            v += step
    return out


def compute_hwp_sequence(
    z_in: float, z_out: float, phis: list[float]
) -> dict[str, tuple[list[float], list[float]]]:
    """Stage-angle lists for co- and cross-polarized runs.

    z_in / z_out: calibrated fast-axis stage angles (deg) of the incoming
    (OptoSigma) and outgoing (Thorlabs) HWPs. Returns
    {"co": (opto, tl), "cross": (opto, tl)} with one entry per phi.
    """
    opto = [z_in + p / 2.0 for p in phis]
    co_tl = [z_out + p / 2.0 for p in phis]
    cross_tl = [z_out + p / 2.0 + 45.0 for p in phis]
    return {"co": (opto, co_tl), "cross": (opto, cross_tl)}


def format_angle_line(angles: list[float]) -> str:
    """Comma-separated 3-decimal line, pasteable into the dual sequencer's
    Manual angle box (which splits on commas and float()s each token)."""
    return ", ".join(f"{a:.3f}" for a in angles)


# ── calibration records ─────────────────────────────────────────────

RECORDS_FILE = pathlib.Path(__file__).parent / "hwp_calibration.json"
ROLES = ("incoming", "outgoing")
ROLE_STAGE = {"incoming": "optosigma", "outgoing": "thorlabs"}


def load_records(path=None) -> dict:
    """Stored calibration records by role. Empty dict on missing or
    unreadable file — partial state (one role calibrated) is normal.

    path=None resolves to RECORDS_FILE at call time, so tests can repoint
    the module attribute without touching the real file."""
    path = pathlib.Path(path if path is not None else RECORDS_FILE)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return {k: v for k, v in data.items() if k in ROLES and isinstance(v, dict)}
    except Exception as e:
        logger.warning(f"could not read {path}: {e}; starting with no records")
        return {}


def save_record(role: str, record: dict, path=None) -> None:
    """Merge one role's record into the JSON file (atomic replace)."""
    path = pathlib.Path(path if path is not None else RECORDS_FILE)
    records = load_records(path)
    records[role] = record
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2))
    tmp.replace(path)


def make_record(stage: str, fit: dict, csv_path: str) -> dict:
    """Persistable record from a fit_waveplate result."""
    amp4 = fit["amp4"]
    return {
        "stage": stage,
        "peak_angle_deg": float(fit["peak_angle"]),
        "r2": float(fit["r2"]),
        "visibility": float(fit["visibility"]),
        "amp2_over_amp4": (float(fit["amp2"] / amp4) if amp4 else None),
        "csv_path": str(csv_path),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def parse_scan_csv(path) -> tuple[np.ndarray, np.ndarray]:
    """Read an angle/power calibration scan CSV (the waveplate_scan.py /
    calibrate-panel output format). Raises ValueError on the wrong shape."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows or [c.strip() for c in rows[0][:2]] != ["angle_deg", "power_W"]:
        raise ValueError(
            f"{path}: expected a CSV with header 'angle_deg,power_W' "
            f"(the calibration-scan output format)"
        )
    ang, pw = [], []
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip():
            ang.append(float(row[0]))
            pw.append(float(row[1]))
    return np.asarray(ang), np.asarray(pw)
