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
