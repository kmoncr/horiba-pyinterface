"""Hardware: the bug (d) regression — OptoSigma angle survives RTC
open and close.

Specifically: after the in-process refactor, opening LiveViewWindow
on the live controller and closing it MUST not cause the controller
to read 0° from the rotation stage. Prior to commit 8/8b, the
launch_external_tool subprocess path tore down the OptoSigma
connection and connect_hardware never brought it back, so the angle
display dropped to 0.
"""

from __future__ import annotations

import asyncio
import threading

import pytest


def test_optosigma_angle_unchanged_across_rtc_open_close(qtbot, real_controller):
    """Open RTC on the live controller, close it, confirm the stage
    reports the same physical angle (within one OSMS-60YAW pulse)."""
    from rtc import LiveViewWindow

    # The GUI background loop pattern: a daemon thread running an
    # asyncio loop. The hardware controller's _sdk_lock is bound to
    # the first loop that uses it, so we set up the loop FIRST.
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    try:
        # Connect on the GUI loop (matches MainWindow's startup).
        fut = asyncio.run_coroutine_threadsafe(
            real_controller.connect_hardware(), loop
        )
        fut.result(timeout=30)

        # Read the starting angle through the controller (this is
        # exactly what the GUI does to populate the angle display).
        fut = asyncio.run_coroutine_threadsafe(
            real_controller.get_rotation_angle(), loop
        )
        before = fut.result(timeout=5)

        # Open and close RTC, exactly as MainWindow._open_rtc_window does.
        win = LiveViewWindow(controller=real_controller, loop=loop)
        qtbot.addWidget(win)
        win.show()
        win.close()

        # Read the angle again. If the bug had returned, the OptoSigma
        # would now be disconnected and the property would short-circuit
        # to 0.0.
        fut = asyncio.run_coroutine_threadsafe(
            real_controller.get_rotation_angle(), loop
        )
        after = fut.result(timeout=5)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)

    # 0.0025°/pulse → tolerance of one pulse is enough.
    assert abs(after - before) < 0.005, (
        f"OptoSigma angle drifted across RTC open/close: "
        f"before={before:.4f}° after={after:.4f}°"
    )
