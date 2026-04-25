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


# ── x-axis combo + conversions (commit 13) ────────────────────────────

import numpy as np
import pytest


@pytest.mark.parametrize("mode,exc_nm,wl_nm,expected", [
    ("Wavelength (nm)",      532.0, 600.0, 600.0),
    ("Energy (eV)",          532.0, 600.0, 1239.841984 / 600.0),
    # Raman shift in cm⁻¹
    ("Raman shift (cm⁻¹)", 532.0, 600.0,
        (1.0/532.0 - 1.0/600.0) * 1e7),
    # Raman shift in eV
    ("Raman shift (eV)",     532.0, 600.0,
        1239.841984/532.0 - 1239.841984/600.0),
])
def test_convert_x_units(qtbot, background_loop, fake_controller,
                         mode, exc_nm, wl_nm, expected):
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)
    win.excitation_wavelength.setValue(exc_nm)
    win.x_axis_combo.setCurrentText(mode)

    arr = np.array([wl_nm], dtype=float)
    converted, label = win._convert_x(arr)
    assert converted.shape == (1,)
    assert converted[0] == pytest.approx(expected, rel=1e-9)
    assert label == mode


def test_x_axis_combo_persists_across_restart(qtbot, mock_horiba_sdk, monkeypatch):
    """Changing the axis combo persists; a new RTC instance restores it."""
    from rtc import LiveViewWindow
    import asyncio, threading

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        from horibacontroller import HoribaController
        # Stages disabled via the mock_horiba_sdk fixture's monkeypatch.
        ctrl = HoribaController(
            enable_logging=False,
            enable_rotation_stage=False,
            enable_thorlabs_stage=False,
        )

        win1 = LiveViewWindow(controller=ctrl, loop=loop)
        qtbot.addWidget(win1)
        win1.x_axis_combo.setCurrentText("Energy (eV)")
        # Persistence wired below should auto-save on this change.

        win2 = LiveViewWindow(controller=ctrl, loop=loop)
        qtbot.addWidget(win2)
        assert win2.x_axis_combo.currentText() == "Energy (eV)"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)


# ── Mode combo + QStackedWidget (commit 15) ───────────────────────────

def test_rtc_has_mode_combo_with_spectrum_and_image(qtbot, background_loop, fake_controller):
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    items = [win.mode_combo.itemText(i) for i in range(win.mode_combo.count())]
    assert "Spectrum" in items
    assert "Image" in items


def test_rtc_mode_combo_swaps_stacked_widget(qtbot, background_loop, fake_controller):
    """Switching the mode combo to Image must show the pg.ImageView;
    Spectrum must show the pg.PlotWidget."""
    import pyqtgraph as pg
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    win.mode_combo.setCurrentText("Spectrum")
    current_spec = win.plot_stack.currentWidget()
    assert isinstance(current_spec, pg.PlotWidget)

    win.mode_combo.setCurrentText("Image")
    current_img = win.plot_stack.currentWidget()
    assert isinstance(current_img, pg.ImageView)


def test_rtc_image_mode_uses_acquire_image(qtbot, background_loop, fake_controller):
    """In Image mode, the scan loop must call controller.acquire_image,
    not acquire_spectrum, and the result must arrive on image_ready."""
    import numpy as np
    from rtc import LiveViewWindow

    expected = np.arange(20, dtype=float).reshape(4, 5)
    fake_controller.acquire_image.return_value = expected

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)
    win.mode_combo.setCurrentText("Image")

    # Wait for one image to come back.
    with qtbot.waitSignal(win.image_ready, timeout=3000):
        win.start_scan()
    win.stop_scan()

    fake_controller.acquire_image.assert_awaited()
    fake_controller.acquire_spectrum.assert_not_awaited()


def test_autoscale_toggle_freezes_view(qtbot, background_loop, fake_controller):
    """When unchecked, pushing data outside the current view range
    must NOT change the visible range. When re-checked, the next data
    push autoscales again."""
    import pyqtgraph as pg
    from rtc import LiveViewWindow

    win = LiveViewWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    # Establish a known range with autoscale enabled.
    win.autoscale_button.setChecked(True)
    win.update_plot([0, 1, 2], [10, 20, 30])
    QApplication = __import__("PyQt5.QtWidgets", fromlist=["QApplication"]).QApplication
    QApplication.processEvents()
    vb = win.plot_item.getViewBox()
    initial_range = vb.viewRange()

    # Disable autoscale and push data far outside the current view.
    win.autoscale_button.setChecked(False)
    QApplication.processEvents()
    win.update_plot([0, 1, 2], [10000, 20000, 30000])
    QApplication.processEvents()
    frozen_range = vb.viewRange()
    assert frozen_range == initial_range, (
        f"autoscale OFF must not change view range: {initial_range} -> {frozen_range}"
    )

    # Re-enable; the range should now adapt.
    win.autoscale_button.setChecked(True)
    QApplication.processEvents()
    win.update_plot([0, 1, 2], [10000, 20000, 30000])
    QApplication.processEvents()
    new_range = vb.viewRange()
    assert new_range != frozen_range
