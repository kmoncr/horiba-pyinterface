"""Hardware: a single short image-mode acquisition."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from horibaprocedure import GAIN_CHOICES, SPEED_CHOICES


def test_acquire_image_full_chip_returns_2d_ndarray(real_controller):
    """Image-mode acquire must return a 2-D ndarray sized to the
    chip dimensions reported by get_configuration."""
    async def main():
        await real_controller.connect_hardware()
        cfg = await real_controller.ccd.get_configuration()
        arr = await real_controller.acquire_image(
            exposure=0.05,
            gain=GAIN_CHOICES['Best Dynamic Range'].value,
            speed=SPEED_CHOICES['50 kHz'].value,
        )
        return cfg, arr

    cfg, arr = asyncio.run(main())
    assert isinstance(arr, np.ndarray)
    assert arr.ndim == 2
    assert arr.shape == (int(cfg["chipHeight"]), int(cfg["chipWidth"]))
