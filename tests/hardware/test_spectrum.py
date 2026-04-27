"""Hardware: a single short spectrum acquisition."""

from __future__ import annotations

import asyncio

import pytest

from horibaprocedure import GAIN_CHOICES, SPEED_CHOICES, GRATING_CHOICES


def test_acquire_spectrum_returns_xy_arrays(real_controller):
    """A 50 ms acquire must return non-empty x and y of equal length.

    Defaults: third grating (150 g/mm), 0.1 mm slit, 545 nm center.
    These are safe values that match the GUI's defaults.
    """
    async def main():
        await real_controller.connect_hardware()
        return await real_controller.acquire_spectrum(
            center_wavelength=545.0,
            exposure=0.05,
            grating=GRATING_CHOICES['Third (150 grooves/mm)'].value,
            slit_position=0.1,
            gain=GAIN_CHOICES['Best Dynamic Range'].value,
            speed=SPEED_CHOICES['50 kHz'].value,
        )

    x, y = asyncio.run(main())

    # Some ICL versions wrap the data one extra time.
    if isinstance(x, list) and len(x) == 1 and isinstance(x[0], list):
        x = x[0]
    if isinstance(y, list) and len(y) == 1 and isinstance(y[0], list):
        y = y[0]

    assert len(x) > 0
    assert len(y) == len(x)
