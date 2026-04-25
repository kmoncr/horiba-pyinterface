"""Tests that exercise horibagui.MainWindow directly.

MainWindow inherits from pymeasure ManagedWindow which builds a
non-trivial widget tree, so these tests are slower than the rest of
the suite. The mock_horiba_sdk fixture keeps the SDK calls hermetic.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def main_window(qtbot, mock_horiba_sdk, monkeypatch):
    """Construct a fully-built MainWindow on the mocked SDK.

    Stages are disabled at construction so we don't try to open serial
    ports during the test. Uses the monkeypatch fixture so the
    HoribaController.__init__ override is undone at teardown — without
    this, later tests that exercise stage handling broke.
    """
    import horibacontroller

    real_init = horibacontroller.HoribaController.__init__

    def init_no_stages(self, *args, **kwargs):
        kwargs["enable_rotation_stage"] = False
        kwargs["enable_thorlabs_stage"] = False
        kwargs.setdefault("enable_logging", False)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(
        horibacontroller.HoribaController, "__init__", init_no_stages
    )

    from horibagui import MainWindow

    win = MainWindow()
    qtbot.addWidget(win)
    return win


def test_main_window_constructs(main_window):
    """Smoke test: MainWindow with mocked SDK builds without error."""
    assert main_window is not None
    assert main_window.controller is not None


# ── Temp-poll suspension during scans (commit 9) ──────────────────────

def test_temp_timer_suspended_when_manager_running(main_window):
    """While the queue is running, trigger_temperature_update must
    stop the timer rather than just returning."""
    from unittest.mock import MagicMock

    main_window.manager = MagicMock()
    main_window.manager.is_running.return_value = True
    main_window.temp_timer.start(5000)
    assert main_window.temp_timer.isActive()

    main_window.trigger_temperature_update()
    assert not main_window.temp_timer.isActive()


def test_temp_timer_suspended_during_rtc_scan(main_window):
    """When the RTC child emits scanning_changed(True) the temp timer
    must pause; on False it must resume."""
    main_window.temp_timer.start(5000)
    assert main_window.temp_timer.isActive()

    main_window._on_child_scanning_changed(True)
    assert not main_window.temp_timer.isActive()

    main_window._on_child_scanning_changed(False)
    assert main_window.temp_timer.isActive()
