"""Tests for image.ImageWindow."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import numpy as np
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
    c = MagicMock(name="FakeController")
    c.is_connected = True
    # Default: returns a small 2D image.
    c.acquire_image = AsyncMock(return_value=np.arange(32, dtype=float).reshape(4, 8))
    c.acquisition_abort = AsyncMock()
    c.shutdown = AsyncMock()
    c.connect_hardware = AsyncMock()
    return c


def test_image_window_construction_does_not_call_shutdown(qtbot, background_loop, fake_controller):
    from image import ImageWindow

    win = ImageWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)
    win.close()

    fake_controller.shutdown.assert_not_called()


def test_image_window_has_pg_image_view(qtbot, background_loop, fake_controller):
    """The window must use pg.ImageView for 2D display."""
    import pyqtgraph as pg
    from image import ImageWindow

    win = ImageWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    assert hasattr(win, "image_view")
    assert isinstance(win.image_view, pg.ImageView)


def test_image_window_acquire_emits_2d(qtbot, background_loop, fake_controller):
    """Clicking Acquire pushes a 2D ndarray into the image view."""
    from image import ImageWindow

    expected = np.arange(32, dtype=float).reshape(4, 8)
    fake_controller.acquire_image.return_value = expected

    win = ImageWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    with qtbot.waitSignal(win.image_ready, timeout=3000):
        win.acquire_button.click()

    fake_controller.acquire_image.assert_awaited()
    # The ImageView must have received the 2D array.
    assert win.image_view.image is not None
    np.testing.assert_array_equal(win.image_view.image, expected)


def test_image_window_passes_roi_when_custom(qtbot, background_loop, fake_controller):
    """Toggling ROI mode to Custom and changing the spinboxes must
    forward those values to controller.acquire_image."""
    from image import ImageWindow

    win = ImageWindow(controller=fake_controller, loop=background_loop)
    qtbot.addWidget(win)

    # Switch to Custom ROI and set values.
    win.roi_mode_combo.setCurrentText("Custom")
    win.x_origin_spin.setValue(10)
    win.y_origin_spin.setValue(20)
    win.x_size_spin.setValue(64)
    win.y_size_spin.setValue(32)

    with qtbot.waitSignal(win.image_ready, timeout=3000):
        win.acquire_button.click()

    kwargs = fake_controller.acquire_image.call_args.kwargs
    assert kwargs.get("x_origin") == 10
    assert kwargs.get("y_origin") == 20
    assert kwargs.get("x_size") == 64
    assert kwargs.get("y_size") == 32
