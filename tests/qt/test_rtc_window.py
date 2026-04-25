"""Tests for rtc.LiveViewWindow."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def background_loop():
    """Spin an asyncio event loop in a daemon thread, like the GUI does.

    Yields the loop. Stops cleanly on teardown.
    """
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
    """A controller-shaped MagicMock with the awaitables RTC uses."""
    c = MagicMock(name="FakeController")
    c.is_connected = True
    c.last_angle = 0.0
    c.enable_thorlabs_stage = False
    c.thorlabs_stage = None
    c.acquire_spectrum = AsyncMock(return_value=([0.0, 1.0], [10.0, 20.0]))
    c.acquire_image = AsyncMock()
    c.acquisition_abort = AsyncMock()
    c.set_rotation_angle = AsyncMock()
    c.get_rotation_angle = AsyncMock(return_value=0.0)
    c.shutdown = AsyncMock()
    c.connect_hardware = AsyncMock()
    return c


def test_injected_rtc_does_not_call_shutdown(qtbot, background_loop, fake_controller):
    """Constructing LiveViewWindow with an injected controller must not
    call the controller's shutdown on close — that is what kills ICL
    and forces the slow ~10 s reconnect when going RTC → main."""
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)
    win.close()

    fake_controller.shutdown.assert_not_called()


def test_injected_rtc_does_not_start_its_own_loop(qtbot, background_loop, fake_controller):
    """When a loop is injected, RTC must use it — not start a new
    background thread."""
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    # The injected loop is the one we built; the window must reference it.
    assert win.loop is background_loop
    # And it must not have started its own loop_thread.
    assert getattr(win, "loop_thread", None) is None


def test_injected_rtc_does_not_call_connect_hardware(qtbot, background_loop, fake_controller):
    """If the caller already has a connected controller, RTC must
    not re-run connect_hardware on construction."""
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    fake_controller.connect_hardware.assert_not_called()


def test_rtc_emits_scanning_changed_on_start_and_stop(qtbot, background_loop, fake_controller):
    """The main window relies on this signal to disable its queue
    while a live scan is running."""
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    with qtbot.waitSignal(win.scanning_changed, timeout=1000) as blocker:
        win.start_scan()
    assert blocker.args == [True]

    with qtbot.waitSignal(win.scanning_changed, timeout=2000) as blocker:
        win.stop_scan()
    assert blocker.args == [False]
