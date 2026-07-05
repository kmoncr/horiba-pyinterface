#!/usr/bin/env python
"""Sweep a waveplate on the OptoSigma rotation stage and read transmitted power
through a fixed polarizer with a Thorlabs PM100A.

Setup:  laser -> waveplate (on OptoSigma stage) -> fixed polarizer -> PM100A

At each angle the stage is moved, allowed to settle, and the power meter is read
(averaged over --navg samples). Power-vs-angle is plotted live. On completion the
data is written to CSV and fit to both a half-wave-plate model (90 deg period) and
a quarter-wave-plate model (180 deg period); the better fit and the fast-axis angle
are printed.

Example:
    uv run python waveplate_scan.py
    uv run python waveplate_scan.py --start 0 --stop 360 --step 5 --navg 10
"""

import argparse
import csv
import time
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt

import pyvisa
from ThorlabsPM100 import ThorlabsPM100

from optosigmacontroller import OptoSigmaController


# Thorlabs USB vendor id, present in the VISA resource string of a PM100x.
THORLABS_VENDOR_ID = "0x1313"


def find_pm100(rm: pyvisa.ResourceManager) -> str:
    """Return the VISA resource string of the first Thorlabs USB power meter."""
    resources = rm.list_resources()
    for res in resources:
        low = res.lower()
        if "usb" in low and (THORLABS_VENDOR_ID in low or "::4883::" in low):
            return res
    raise RuntimeError(
        f"No Thorlabs USB power meter found. VISA resources seen: {resources}. "
        f"Pass one explicitly with --visa."
    )


def read_power(pm: ThorlabsPM100, navg: int) -> float:
    """Average navg power reads (Watts)."""
    vals = []
    for _ in range(navg):
        vals.append(pm.read)
    return float(np.mean(vals))


def fit_harmonic(angles_deg: np.ndarray, power: np.ndarray, k: int):
    """Linear least-squares fit of  P = D + a*cos(k*theta) + b*sin(k*theta).

    Both waveplate models reduce to a single harmonic:
      HWP:  A*cos^2(2*(t-t0)) + C  ->  k = 4  (90 deg period)
      QWP:  A*cos^2(   (t-t0)) + C  ->  k = 2  (180 deg period)

    Returns dict with fit params, R^2, and the peak/min (fast/slow-axis) angles.
    """
    t = np.deg2rad(angles_deg)
    M = np.column_stack([np.ones_like(t), np.cos(k * t), np.sin(k * t)])
    coeffs, *_ = np.linalg.lstsq(M, power, rcond=None)
    D, a, b = coeffs
    model = M @ coeffs

    ss_res = np.sum((power - model) ** 2)
    ss_tot = np.sum((power - power.mean()) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    period = 360.0 / k
    # Modulation peaks where k*theta == phase.
    phase = np.arctan2(b, a)  # radians
    peak = (np.rad2deg(phase) / k) % period
    trough = (peak + period / 2.0) % period
    amplitude = np.hypot(a, b)  # half peak-to-peak of the modulation

    return {
        "k": k,
        "D": D,
        "a": a,
        "b": b,
        "amplitude": amplitude,
        "offset": D,
        "period": period,
        "r2": r2,
        "peak_angle": peak,
        "trough_angle": trough,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--visa",
        default=None,
        help="VISA resource string for the PM100A (auto-detect if omitted)",
    )
    p.add_argument("--port", default="COM3", help="OptoSigma stage serial port")
    p.add_argument("--start", type=float, default=0.0, help="start angle (deg)")
    p.add_argument(
        "--stop", type=float, default=180.0, help="stop angle, inclusive (deg)"
    )
    p.add_argument("--step", type=float, default=2.0, help="angle step (deg)")
    p.add_argument(
        "--settle",
        type=float,
        default=0.3,
        help="settle time after each move before reading (s)",
    )
    p.add_argument(
        "--navg", type=int, default=5, help="power reads to average per point"
    )
    p.add_argument("--out", default=None, help="output CSV path")
    args = p.parse_args()

    out_path = args.out or f"waveplate_scan_{datetime.now():%Y%m%d_%H%M%S}.csv"

    # --- connect power meter ---
    rm = pyvisa.ResourceManager()
    resource = args.visa or find_pm100(rm)
    print(f"connecting to power meter: {resource}")
    inst = rm.open_resource(resource)
    inst.timeout = 5000
    pm = ThorlabsPM100(inst)

    # --- connect rotation stage ---
    print(f"connecting to OptoSigma stage on {args.port}")
    stage = OptoSigmaController(port=args.port)
    if not stage.connect():
        raise RuntimeError(f"failed to connect to OptoSigma stage on {args.port}")

    angles = np.arange(args.start, args.stop + args.step / 2.0, args.step)

    # --- live plot ---
    plt.ion()
    fig, ax = plt.subplots()
    (line,) = ax.plot([], [], "o-", ms=4)
    ax.set_xlabel("waveplate angle (deg)")
    ax.set_ylabel("power (W)")
    fig.show()

    measured_angles = []
    powers = []

    try:
        for target in angles:
            stage.degree = float(target)
            time.sleep(args.settle)
            power = read_power(pm, args.navg)
            actual = stage.degree

            measured_angles.append(actual)
            powers.append(power)
            print(f"  {actual:7.2f} deg   {power:.4e} W")

            line.set_data(measured_angles, powers)
            ax.relim()
            ax.autoscale_view()
            fig.canvas.draw_idle()
            plt.pause(0.001)
    except KeyboardInterrupt:
        print("\ninterrupted; saving what we have so far")
    finally:
        stage.disconnect()
        inst.close()

    if not powers:
        print("no data collected")
        return

    # --- save CSV ---
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["angle_deg", "power_W"])
        w.writerows(zip(measured_angles, powers))
    print(f"\nsaved {len(powers)} points to {out_path}")

    # --- fit both models ---
    ang = np.asarray(measured_angles)
    pw = np.asarray(powers)
    hwp = fit_harmonic(ang, pw, k=4)
    qwp = fit_harmonic(ang, pw, k=2)

    print("\nfit results (P = offset + modulation):")
    for name, fit in (("HWP (90 deg period)", hwp), ("QWP (180 deg period)", qwp)):
        print(
            f"  {name}: R^2 = {fit['r2']:.4f}, "
            f"amplitude = {fit['amplitude']:.4e} W, offset = {fit['offset']:.4e} W"
        )

    best_name, best = max((("HWP", hwp), ("QWP", qwp)), key=lambda nf: nf[1]["r2"])
    print(f"\nbest fit: {best_name}  (R^2 = {best['r2']:.4f})")
    print(
        f"  max transmission (waveplate axis || polarizer) at "
        f"{best['peak_angle']:.2f} deg  (mod {best['period']:.0f} deg)"
    )
    print(
        f"  min transmission (crossed) at "
        f"{best['trough_angle']:.2f} deg  (mod {best['period']:.0f} deg)"
    )
    print(
        "  -> fast axis lies along one of these extrema; which is fast vs slow "
        "depends on the input polarization orientation."
    )

    # overlay best fit on the live plot
    fine = np.linspace(ang.min(), ang.max(), 500)
    t = np.deg2rad(fine)
    model = (
        best["D"]
        + best["a"] * np.cos(best["k"] * t)
        + best["b"] * np.sin(best["k"] * t)
    )
    ax.plot(fine, model, "-", lw=1.5, label=f"{best_name} fit")
    ax.legend()
    fig.canvas.draw_idle()

    print("\nclose the plot window to exit.")
    plt.ioff()
    plt.show()


if __name__ == "__main__":
    main()
