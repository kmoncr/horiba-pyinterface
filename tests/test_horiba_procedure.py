"""Tests for HoribaSpectrumProcedure."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def fake_loop():
    """Background asyncio loop for the procedure's run_async helper."""
    import threading
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


def test_data_columns_include_energy_and_raman_energy():
    from horibaprocedure import HoribaSpectrumProcedure

    cols = HoribaSpectrumProcedure.DATA_COLUMNS
    assert "Wavenumber" in cols
    assert "Wavelength" in cols
    assert "Intensity" in cols
    assert "Scan Number" in cols
    assert "Energy" in cols
    assert "Raman Energy" in cols


def test_execute_emits_energy_and_raman_energy(fake_loop):
    """Per-pixel emit dicts must include Energy = 1239.84 / lambda_nm
    and Raman Energy = E_excitation - E_pixel."""
    from horibaprocedure import HoribaSpectrumProcedure

    proc = HoribaSpectrumProcedure()
    proc.controller = MagicMock()
    proc.controller.enable_thorlabs_stage = False
    proc.controller.thorlabs_stage = None
    proc.controller.set_rotation_angle = AsyncMock()
    proc.controller.acquire_spectrum = AsyncMock(
        return_value=([500.0, 600.0], [10.0, 20.0])
    )
    proc.loop = fake_loop

    proc.excitation_wavelength = 532.0
    proc.center_wavelength = 545.0
    proc.exposure = 0.1
    proc.slit_position = 0.1
    proc.gain = 'Best Dynamic Range'
    proc.speed = '50 kHz'
    proc.grating = 'Third (150 grooves/mm)'
    proc.rotation_angle = 0.0
    proc.thorlabs_angle = 0.0
    proc.scan_number = 1
    proc.ccd_y_origin = 0
    proc.ccd_y_size = 256
    proc.ccd_x_bin = 1

    captured = []
    proc.emit = lambda topic, payload: captured.append((topic, payload))
    proc.should_stop = lambda: False

    proc.execute()

    assert len(captured) == 2
    HC = 1239.841984
    e_excitation = HC / 532.0

    for (topic, payload), wl in zip(captured, [500.0, 600.0]):
        assert topic == "results"
        assert payload["Wavelength"] == wl
        assert payload["Energy"] == pytest.approx(HC / wl, rel=1e-9)
        assert payload["Raman Energy"] == pytest.approx(e_excitation - HC / wl, rel=1e-9)


def test_enumconv_round_trips_all_choices():
    """Every label in GRATING_CHOICES / GAIN_CHOICES / SPEED_CHOICES
    must round-trip through enumconv to its underlying enum value."""
    from horibaprocedure import (
        HoribaSpectrumProcedure,
        GRATING_CHOICES, GAIN_CHOICES, SPEED_CHOICES,
    )

    proc = HoribaSpectrumProcedure()
    for label, enum_val in GRATING_CHOICES.items():
        assert proc.enumconv("grating", label) == enum_val.value
    for label, enum_val in GAIN_CHOICES.items():
        assert proc.enumconv("gain", label) == enum_val.value
    for label, enum_val in SPEED_CHOICES.items():
        assert proc.enumconv("speed", label) == enum_val.value
