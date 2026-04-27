"""Fixtures for the live-hardware test suite.

These tests talk to the real ICL, spectrometer, and OptoSigma stage.
They are skipped by default. Run with::

    uv run python -m pytest tests/hardware/ --run-hardware -v

Each test:
  * records initial state where it can,
  * uses small exposures (≤ 100 ms) to keep runtime sane,
  * tries to leave the hardware at its starting state on teardown.

Tests do NOT exercise the Thorlabs K10CR2 — that stage is optional
on this rig and may not be powered.
"""

from __future__ import annotations

import asyncio

import pytest


def pytest_collection_modifyitems(config, items):
    """Auto-skip hardware tests unless --run-hardware was passed.

    The hook lives in this conftest so it applies only to items
    collected from the tests/hardware/ directory; we still filter on
    path because pytest delivers the full session's items list to
    every conftest's hook.
    """
    if config.getoption("--run-hardware"):
        return
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    skip_marker = pytest.mark.skip(
        reason="hardware tests disabled (pass --run-hardware to enable)"
    )
    for item in items:
        item_path = os.path.abspath(str(item.fspath))
        if item_path.startswith(here):
            item.add_marker(skip_marker)


@pytest.fixture
def real_controller():
    """A live HoribaController bound to the real SDK + OptoSigma stage.

    Yields the controller, then runs a full shutdown so subsequent
    tests start from a clean ICL state.
    """
    from horibacontroller import HoribaController

    c = HoribaController(
        enable_logging=False,
        enable_rotation_stage=True,
        enable_thorlabs_stage=False,  # K10CR2 is optional and may be off.
    )
    try:
        yield c
    finally:
        try:
            asyncio.run(c.shutdown())
        except Exception:
            pass


# NOTE: there is no `connected` fixture on purpose. The controller's
# asyncio.Lock is bound to whichever loop first uses it; running
# connect_hardware in one asyncio.run() and then acquire_spectrum in a
# second would re-use a Lock bound to the dead first loop. Hardware
# tests therefore consolidate all awaits into a single asyncio.run().
