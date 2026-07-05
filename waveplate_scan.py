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


def fit_waveplate(angles_deg: np.ndarray, power: np.ndarray):
    """Fit power vs waveplate angle for a retarder between fixed polarizers.

    For fixed linear input -> rotating retarder (fast axis at theta) -> fixed
    polarizer, the transmitted power is

        P(theta) = C + A*cos(4*(theta - theta0))              (90 deg period)

    for ANY retardance: a HWP and a QWP give the SAME 4*theta functional form,
    differing only in modulation amplitude (and full-depth when crossed). So the
    curve cannot tell HWP from QWP -- it only locates the axis.

    A small 2*theta term is also fit to capture peak-height asymmetry (slight
    input ellipticity or laser drift over the scan); ideally it is ~zero.

        P(theta) = C + A4*cos(4*(theta - t4)) + A2*cos(2*(theta - t2))

    Returns a dict with the fit params, R^2, the extremum (axis) angles of the
    4*theta term, and the measured modulation visibility.
    """
    t = np.deg2rad(angles_deg)
    M = np.column_stack(
        [
            np.ones_like(t),
            np.cos(4 * t),
            np.sin(4 * t),
            np.cos(2 * t),
            np.sin(2 * t),
        ]
    )
    coeffs, *_ = np.linalg.lstsq(M, power, rcond=None)
    C, a4, b4, a2, b2 = coeffs
    model = M @ coeffs

    ss_res = np.sum((power - model) ** 2)
    ss_tot = np.sum((power - power.mean()) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # 4*theta term: peak where 4*theta == phase.
    amp4 = np.hypot(a4, b4)
    peak = (np.rad2deg(np.arctan2(b4, a4)) / 4.0) % 90.0
    trough = (peak + 45.0) % 90.0

    # 2*theta term amplitude, as a diagnostic of asymmetry.
    amp2 = np.hypot(a2, b2)

    pmax, pmin = float(power.max()), float(power.min())
    visibility = (pmax - pmin) / (pmax + pmin) if (pmax + pmin) > 0 else 0.0

    return {
        "C": C,
        "a4": a4,
        "b4": b4,
        "a2": a2,
        "b2": b2,
        "amp4": amp4,
        "amp2": amp2,
        "r2": r2,
        "peak_angle": peak,
        "trough_angle": trough,
        "visibility": visibility,
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

    # --- fit ---
    ang = np.asarray(measured_angles)
    pw = np.asarray(powers)
    fit = fit_waveplate(ang, pw)

    print(
        f"\nfit: P = C + A4*cos(4(theta - t0)) + A2*cos(2(...))   R^2 = {fit['r2']:.4f}"
    )
    print(f"  4-theta (90 deg period) amplitude A4 = {fit['amp4']:.4e} W")
    print(
        f"  2-theta asymmetry term    amplitude A2 = {fit['amp2']:.4e} W "
        f"(A2/A4 = {fit['amp2'] / fit['amp4']:.2%} -- large => input ellipticity or drift)"
    )
    print(f"  modulation visibility = {fit['visibility']:.3f}  (1.0 = dips to zero)")
    print(
        f"\n  max transmission (waveplate axis || polarizer) at "
        f"{fit['peak_angle']:.2f} deg  (mod 90 deg)"
    )
    print(
        f"  min transmission (crossed)                    at "
        f"{fit['trough_angle']:.2f} deg  (mod 90 deg)"
    )
    print(
        "\n  NOTE: a HWP and a QWP both give this same 90-deg-period cos(4*theta)\n"
        "  curve between fixed polarizers -- this scan locates the axis but does\n"
        "  NOT identify the plate type. The fast axis is one of the extrema above;\n"
        "  which extremum is fast vs slow depends on the input polarization."
    )

    # overlay fit on the live plot
    fine = np.linspace(ang.min(), ang.max(), 500)
    t = np.deg2rad(fine)
    model = (
        fit["C"]
        + fit["a4"] * np.cos(4 * t)
        + fit["b4"] * np.sin(4 * t)
        + fit["a2"] * np.cos(2 * t)
        + fit["b2"] * np.sin(2 * t)
    )
    ax.plot(fine, model, "-", lw=1.5, label="fit")
    ax.legend()
    fig.canvas.draw_idle()

    print("\nclose the plot window to exit.")
    plt.ioff()
    plt.show()


if __name__ == "__main__":
    main()
