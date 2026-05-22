"""Hardware: CCD temperature read."""

from __future__ import annotations

import asyncio


def test_get_ccd_temperature_returns_negative(real_controller):
    """The Synapse Plus CCD is cooled to roughly -70 °C in operation;
    a successful read returns a negative number, definitely not 0
    (uninitialised) or -999.0 (the error sentinel)."""
    async def main():
        await real_controller.connect_hardware()
        return await real_controller.get_ccd_temperature()

    temp = asyncio.run(main())
    assert temp != -999.0, "temperature read returned the error sentinel"
    assert temp != 0.0, "temperature read returned 0.0 (uninitialised)"
    assert temp < 0.0, f"expected negative temperature, got {temp}"
