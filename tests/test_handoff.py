"""Regression tests for the in-process Normal <-> RTC <-> Image handoff.

The slow handoff and post-RTC freeze were both caused by the old
launch_external_tool path: it called controller.shutdown() which (with
start_icl=True) shuts down icl.exe, and then spawned the child as a
subprocess that built a fresh DeviceManager.

These tests verify that the new in-process openers never call shutdown
and never call dm.stop on the device manager.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def background_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


@pytest.fixture
def fake_controller():
    """Controller-shaped mock with a fake DeviceManager for stop tracking."""
    c = MagicMock(name="FakeController")
    c.is_connected = True
    c.last_angle = 0.0
    c.enable_thorlabs_stage = False
    c.thorlabs_stage = None
    c.dm = MagicMock(name="FakeDM")
    c.dm.stop = AsyncMock()
    c.acquire_spectrum = AsyncMock(return_value=([0.0, 1.0], [10.0, 20.0]))
    c.acquire_image = AsyncMock()
    c.acquisition_abort = AsyncMock()
    c.set_rotation_angle = AsyncMock()
    c.get_rotation_angle = AsyncMock(return_value=0.0)
    c.shutdown = AsyncMock()
    c.connect_hardware = AsyncMock()
    return c


# ── Direct child-window tests (don't need MainWindow) ────────────────

def test_open_rtc_does_not_call_controller_shutdown(qtbot, background_loop, fake_controller):
    """Opening LiveViewWindow with the shared controller, then closing
    it, must not call controller.shutdown() or dm.stop()."""
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)
    win.show()
    win.close()

    fake_controller.shutdown.assert_not_called()
    fake_controller.dm.stop.assert_not_called()


def test_open_image_does_not_call_controller_shutdown(qtbot, background_loop, fake_controller):
    from image import ImageWindow

    win = ImageWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)
    win.show()
    win.close()

    fake_controller.shutdown.assert_not_called()
    fake_controller.dm.stop.assert_not_called()
