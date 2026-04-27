"""Hardware: ICL + spectrometer + CCD connect/disconnect."""

from __future__ import annotations

import asyncio
import time

import pytest


def test_connect_succeeds(real_controller):
    """connect_hardware completes against the real ICL within 30 s and
    leaves the controller flagged as connected."""
    async def main():
        t0 = time.time()
        await real_controller.connect_hardware()
        return time.time() - t0

    elapsed = asyncio.run(main())
    assert real_controller.is_connected is True
    assert real_controller.mono is not None
    assert real_controller.ccd is not None
    assert elapsed < 30.0, f"connect_hardware took {elapsed:.1f} s (>30 s)"


def test_get_configuration_returns_chip_dimensions(real_controller):
    """The CCD must report a chipWidth / chipHeight so image-mode
    full-chip ROI can default correctly."""
    async def main():
        await real_controller.connect_hardware()
        return await real_controller.ccd.get_configuration()

    cfg = asyncio.run(main())
    assert int(cfg["chipWidth"]) > 0
    assert int(cfg["chipHeight"]) > 0
