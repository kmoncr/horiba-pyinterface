#!/usr/bin/env python
"""Sweep a waveplate on a rotation stage and read transmitted power through a
fixed polarizer with a Thorlabs PM100A.

Setup:  laser -> waveplate (on rotation stage) -> fixed polarizer -> PM100A

The rotation stage can be either the OptoSigma stage (default) or a Thorlabs
K10CR2. At each angle the stage is moved, allowed to settle, and the power meter
is read (averaged over --navg samples). Power-vs-angle is plotted live. On
completion the data is written to CSV and fit to a single 90-deg-period
cos(4*theta) model (the form a HWP or QWP both produce between fixed polarizers)
to locate the fast axis.

Example:
    uv run python waveplate_scan.py                         # OptoSigma on COM3
    uv run python waveplate_scan.py --stage thorlabs --serial 55000000 --home
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
from thorlabscontroller import ThorlabsK10CR2Controller


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


def make_stage(args):
    """Build and connect the rotation stage selected on the command line.

    Both controllers expose the same interface the scan uses: connect(),
    disconnect(), is_connected, and a blocking `degree` get/set property. So the
    scan loop and fit are stage-agnostic -- only construction differs (OptoSigma
    takes a COM port, K10CR2 a serial number), plus the K10CR2 generally needs a
    Home before absolute moves are meaningful.
    """
    if args.stage == "thorlabs":
        if not args.serial:
            raise RuntimeError(
                "--stage thorlabs requires --serial <K10CR2 serial number>"
            )
        print(f"connecting to Thorlabs K10CR2 {args.serial}")
        stage = ThorlabsK10CR2Controller(serial_number=args.serial)
        if not stage.connect():
            raise RuntimeError(f"failed to connect to Thorlabs K10CR2 {args.serial}")
        if args.home:
            print("homing K10CR2 (establishing absolute zero)...")
            stage.home()
        return stage

    print(f"connecting to OptoSigma stage on {args.port}")
    stage = OptoSigmaController(port=args.port)
    if not stage.connect():
        raise RuntimeError(f"failed to connect to OptoSigma stage on {args.port}")
    return stage


def read_power(pm: ThorlabsPM100, navg: int) -> float:
    """Average navg power reads (Watts)."""
    vals = []
    for _ in range(navg):
        vals.append(pm.read)
    return float(np.mean(vals))


def fit_waveplate(angles_deg: np.ndarray, power: np.ndarray, clip_frac: float = 0.05):
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

    Clip-aware: near extinction (crossed) the transmitted power drops below the
    meter's range/noise floor and reads a roughly constant small value, giving
    flat-bottomed minima that no sinusoid can pass through -- an ordinary fit is
    dragged negative at the troughs and underfits the peaks. We detect that floor
    and EXCLUDE the floored points from the least-squares fit, so the harmonic is
    set by the (real) peaks and shoulders. The true extinction lies below the
    floor, so the fitted 4*theta trough may go negative -- that is expected and
    reported, not an error.

    clip_frac: points within clip_frac*(max-min) of the minimum are treated as
    floored and excluded from the fit.

    Returns a dict with the fit params, R^2 (over fitted points), the extremum
    (axis) angles of the 4*theta term, the detected floor, and the modulation
    visibility.
    """
    t = np.deg2rad(angles_deg)
    pmax, pmin = float(power.max()), float(power.min())
    prange = pmax - pmin

    # Flag floored (clipped) points near the minimum and exclude them from the fit.
    floor = pmin
    clipped = (
        power <= pmin + clip_frac * prange if prange > 0 else np.zeros_like(power, bool)
    )
    keep = ~clipped
    # Guard: a harmonic fit needs enough non-clipped points; else fit everything.
    if keep.sum() < 6:
        keep = np.ones_like(power, bool)
        clipped = ~keep

    def design(tt):
        return np.column_stack(
            [
                np.ones_like(tt),
                np.cos(4 * tt),
                np.sin(4 * tt),
                np.cos(2 * tt),
                np.sin(2 * tt),
            ]
        )

    coeffs, *_ = np.linalg.lstsq(design(t[keep]), power[keep], rcond=None)
    C, a4, b4, a2, b2 = coeffs

    # R^2 evaluated over the fitted (non-clipped) points only.
    model_keep = design(t[keep]) @ coeffs
    ss_res = np.sum((power[keep] - model_keep) ** 2)
    ss_tot = np.sum((power[keep] - power[keep].mean()) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # 4*theta term: peak where 4*theta == phase.
    amp4 = np.hypot(a4, b4)
    peak = (np.rad2deg(np.arctan2(b4, a4)) / 4.0) % 90.0
    trough = (peak + 45.0) % 90.0

    # 2*theta term amplitude, as a diagnostic of asymmetry.
    amp2 = np.hypot(a2, b2)

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
        "floor": floor,
        "n_clipped": int(clipped.sum()),
        "n_total": int(power.size),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--visa",
        default=None,
        help="VISA resource string for the PM100A (auto-detect if omitted)",
    )
    p.add_argument(
        "--stage",
        choices=["optosigma", "thorlabs"],
        default="optosigma",
        help="rotation stage type (default: optosigma)",
    )
    p.add_argument("--port", default="COM3", help="OptoSigma stage serial port")
    p.add_argument("--serial", default=None, help="Thorlabs K10CR2 serial number")
    p.add_argument(
        "--home",
        action="store_true",
        help="home the K10CR2 before scanning (ignored for OptoSigma)",
    )
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
    stage = make_stage(args)

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

    if fit["n_clipped"] > 0:
        print(
            f"\n  WARNING: {fit['n_clipped']}/{fit['n_total']} points sit at the "
            f"floor ({fit['floor']:.3e} W) and were excluded from the fit.\n"
        )

    print(
        f"\n  max transmission (waveplate axis || polarizer) at "
        f"{fit['peak_angle']:.2f} deg  (mod 90 deg)"
    )
    print(
        f"  min transmission (crossed)                    at "
        f"{fit['trough_angle']:.2f} deg  (mod 90 deg)"
    )

    # overlay fit on the live plot, clipped at the floor so it hugs the flat
    # bottoms instead of diving negative through the clip-limited minima.
    fine = np.linspace(ang.min(), ang.max(), 500)
    t = np.deg2rad(fine)
    model = (
        fit["C"]
        + fit["a4"] * np.cos(4 * t)
        + fit["b4"] * np.sin(4 * t)
        + fit["a2"] * np.cos(2 * t)
        + fit["b2"] * np.sin(2 * t)
    )
    model_clipped = np.maximum(fit["floor"], model)
    ax.plot(fine, model_clipped, "-", lw=1.5, label="fit")
    ax.legend()
    fig.canvas.draw_idle()

    print("\nclose the plot window to exit.")
    plt.ioff()
    plt.show()


if __name__ == "__main__":
    main()
