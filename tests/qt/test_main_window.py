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


# ── QSettings persistence (commit 10) ─────────────────────────────────

def _build_main_window(qtbot, monkeypatch):
    """Helper used by round-trip tests where we need to construct the
    window twice with the same QSettings storage."""
    import horibacontroller
    real_init = horibacontroller.HoribaController.__init__

    def init_no_stages(self, *args, **kwargs):
        kwargs["enable_rotation_stage"] = False
        kwargs["enable_thorlabs_stage"] = False
        kwargs.setdefault("enable_logging", False)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(horibacontroller.HoribaController, "__init__", init_no_stages)

    from horibagui import MainWindow
    win = MainWindow()
    qtbot.addWidget(win)
    return win


def test_main_window_settings_round_trip(qtbot, mock_horiba_sdk, monkeypatch):
    """Mutate spinboxes and combos; destroy window; build a new one;
    assert every widget reads back the mutated value."""
    win1 = _build_main_window(qtbot, monkeypatch)

    # Mutate persisted widgets.
    win1.inputs.excitation_wavelength.setValue(488.0)
    win1.inputs.center_wavelength.setValue(610.5)
    win1.inputs.exposure.setValue(7.25)
    win1.inputs.slit_position.setValue(0.42)
    win1.inputs.gain.setValue('Ultimate Sensitivity')
    win1.inputs.speed.setValue('1 MHz')
    win1.inputs.ccd_y_origin.setValue(64)
    win1.inputs.ccd_y_size.setValue(128)
    win1.inputs.ccd_x_bin.setValue(2)
    win1.grating_combo.setCurrentText('First (1800 grooves/mm)')
    win1.scans_per_angle_input.setValue(11)

    # Persist now (some widgets save on focus-out; force a flush).
    win1._save_persistent_settings()

    win1.close()

    # Build a fresh window with the same QSettings backing.
    win2 = _build_main_window(qtbot, monkeypatch)

    assert win2.inputs.excitation_wavelength.value() == 488.0
    assert win2.inputs.center_wavelength.value() == 610.5
    assert win2.inputs.exposure.value() == 7.25
    assert win2.inputs.slit_position.value() == 0.42
    assert win2.inputs.gain.value() == 'Ultimate Sensitivity'
    assert win2.inputs.speed.value() == '1 MHz'
    assert win2.inputs.ccd_y_origin.value() == 64
    assert win2.inputs.ccd_y_size.value() == 128
    assert win2.inputs.ccd_x_bin.value() == 2
    assert win2.grating_combo.currentText() == 'First (1800 grooves/mm)'
    assert win2.scans_per_angle_input.value() == 11
