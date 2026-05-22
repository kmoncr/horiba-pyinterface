"""Hardware: OptoSigma rotation stage smoke tests.

Each test records the starting angle, performs a small move, and
restores the original angle on teardown.
"""

from __future__ import annotations

import asyncio

import pytest


def test_optosigma_reads_real_angle(real_controller):
    """get_rotation_angle returns the live OptoSigma position
    (degrees, not pulses, not 0)."""
    async def main():
        return await real_controller.get_rotation_angle()

    angle = asyncio.run(main())
    # Real stage will be somewhere in [0, 360); we just assert the
    # call returned a number rather than blowing up.
    assert isinstance(angle, float)
    assert -360.0 <= angle <= 360.0


def test_optosigma_set_angle_round_trip(real_controller):
    """Move +1°, read back within 0.05° tolerance, then restore."""
    async def main():
        start = await real_controller.get_rotation_angle()
        target = (start + 1.0) % 360.0
        await real_controller.set_rotation_angle(target)
        readback = await real_controller.get_rotation_angle()
        # Restore.
        await real_controller.set_rotation_angle(start)
        return start, target, readback

    start, target, readback = asyncio.run(main())
    # OSMS-60YAW resolution is 0.0025°/pulse, so a move of 1° should
    # land within one or two pulses of target.
    assert abs(readback - target) < 0.05, (
        f"start={start:.3f} target={target:.3f} readback={readback:.3f}"
    )
