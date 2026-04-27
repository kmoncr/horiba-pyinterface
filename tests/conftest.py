"""Shared pytest fixtures for the horiba project.

By default tests run with no real hardware. We monkey-patch the SDK
classes that ``horibacontroller`` imports so that
``HoribaController(...)`` and its methods exercise pure-Python mocks
instead of the ICL websocket.

The opt-in suite under ``tests/hardware/`` is gated behind the
``--run-hardware`` CLI flag and talks to the real ICL + spectrometer
+ stages. Those tests are skipped unless that flag is passed.

QSettings is redirected to a per-test tmp directory so persistence
tests do not pollute the real user registry.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── CLI flag + marker registration ─────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help="run tests in tests/hardware/ that talk to real hardware "
             "(ICL must be installed and devices must be powered on)",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hardware: requires real ICL+spectrometer (use --run-hardware to enable)",
    )

# Mark the entire test session as a mock environment. Production code can
# read this if it ever needs to skip a real hardware path.
os.environ["HORIBA_MOCK"] = "1"

# Make repo root importable so ``import horibacontroller`` works without
# installing the project. pytest's rootdir sets cwd, but not sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── QSettings isolation ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_qsettings(tmp_path, monkeypatch):
    """Redirect QSettings to a per-test ini file.

    ``setPath(IniFormat, UserScope, tmp_path)`` makes every QSettings
    instance constructed with an organisation/application pair write to
    ``tmp_path/<org>/<app>.ini`` instead of the real registry. We force
    IniFormat as the default so widgets that construct QSettings without
    arguments also land in the tmp dir.
    """
    from PyQt5.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    yield


# ── Mock SDK ───────────────────────────────────────────────────────────

def _make_mock_mono():
    """Build a Monochromator mock that exposes the real nested enums.

    horibacontroller.py reaches into ``mono.Slit.A`` / ``mono.Mirror``
    etc., so the mock instance must carry those enum classes. We import
    them from the real SDK (which is installed in the repo's .venv) and
    attach them to the mock.
    """
    from horiba_sdk.devices.single_devices.monochromator import Monochromator

    mono = MagicMock(name="MockMonochromator")
    # Async methods used by horibacontroller
    mono.open = AsyncMock()
    mono.close = AsyncMock()
    mono.is_open = AsyncMock(return_value=True)
    mono.is_initialized = AsyncMock(return_value=True)
    mono.initialize = AsyncMock()
    mono.is_busy = AsyncMock(return_value=False)
    mono.set_turret_grating = AsyncMock()
    mono.move_to_target_wavelength = AsyncMock()
    mono.set_slit_position = AsyncMock()
    mono.set_mirror_position = AsyncMock()
    # ``mono.id()`` is a synchronous getter in the real SDK (ABCDevice.id())
    mono.id = MagicMock(return_value=0)
    # Expose the real nested enums so ``mono.Slit.A`` etc. work.
    mono.Slit = Monochromator.Slit
    mono.Mirror = Monochromator.Mirror
    mono.MirrorPosition = Monochromator.MirrorPosition
    mono.Grating = Monochromator.Grating
    return mono


def _default_acquisition_payload():
    """Spectrum-shape acquisition data: 1024 px, single ROI."""
    x = list(range(1024))
    y = [float(i) for i in range(1024)]
    return [{"roi": [{"xData": x, "yData": y}]}]


def _make_mock_ccd():
    ccd = MagicMock(name="MockCCD")
    ccd.open = AsyncMock()
    ccd.close = AsyncMock()
    ccd.is_open = AsyncMock(return_value=True)
    ccd.get_configuration = AsyncMock(return_value={
        "chipWidth": 1024,
        "chipHeight": 256,
    })
    ccd.set_acquisition_count = AsyncMock()
    ccd.set_center_wavelength = AsyncMock()
    ccd.set_exposure_time = AsyncMock()
    ccd.set_gain = AsyncMock()
    ccd.set_speed = AsyncMock()
    ccd.set_timer_resolution = AsyncMock()
    ccd.set_acquisition_format = AsyncMock()
    ccd.set_region_of_interest = AsyncMock()
    ccd.set_x_axis_conversion_type = AsyncMock()
    ccd.get_acquisition_ready = AsyncMock(return_value=True)
    ccd.acquisition_start = AsyncMock()
    ccd.get_acquisition_busy = AsyncMock(return_value=False)
    ccd.get_acquisition_data = AsyncMock(side_effect=lambda: _default_acquisition_payload())
    ccd.get_chip_temperature = AsyncMock(return_value=-70.0)
    ccd.acquisition_abort = AsyncMock()
    return ccd


def _make_mock_device_manager(mono, ccd):
    """Build a DeviceManager class whose instances expose mono+ccd lists."""

    class _MockDeviceManager:
        def __init__(self, start_icl: bool = True, **_kwargs):
            self.start_icl = start_icl
            self.monochromators = [mono]
            self.charge_coupled_devices = [ccd]
            self.start = AsyncMock()
            self.stop = AsyncMock()
            self.start_icl_method = AsyncMock()
            self.stop_icl = AsyncMock()

    return _MockDeviceManager


@pytest.fixture
def mock_horiba_sdk(monkeypatch):
    """Patch SDK names that ``horibacontroller`` imported.

    Returns a small struct so tests can configure return values on the
    mocks (e.g. ``mock_horiba_sdk.ccd.get_acquisition_data.side_effect = ...``).
    """
    import importlib

    # Make sure the module is loaded once before we patch its globals.
    horibacontroller = importlib.import_module("horibacontroller")

    mono = _make_mock_mono()
    ccd = _make_mock_ccd()
    DeviceManagerCls = _make_mock_device_manager(mono, ccd)

    monkeypatch.setattr(horibacontroller, "DeviceManager", DeviceManagerCls)
    # ChargeCoupledDevice / Monochromator are imported by horibacontroller
    # only for type hints in ``_wait_for_*``. Patch them too so isinstance
    # checks (if any are added later) keep working.
    monkeypatch.setattr(horibacontroller, "ChargeCoupledDevice", type(ccd))
    monkeypatch.setattr(horibacontroller, "Monochromator", type(mono))

    # Disable rotation stages by default so the controller doesn't try to
    # open serial ports during construction. Individual tests can monkey
    # patch the controllers if they need stage behaviour.
    monkeypatch.setattr(horibacontroller, "OptoSigmaController", MagicMock)
    if hasattr(horibacontroller, "ThorlabsK10CR2Controller"):
        monkeypatch.setattr(horibacontroller, "ThorlabsK10CR2Controller", MagicMock)

    class _Bundle:
        pass

    bundle = _Bundle()
    bundle.mono = mono
    bundle.ccd = ccd
    bundle.DeviceManager = DeviceManagerCls
    return bundle


# ── Controller fixture (built on top of mock SDK) ──────────────────────

@pytest.fixture
def controller(mock_horiba_sdk):
    """A HoribaController with stages disabled, SDK mocked, not connected."""
    from horibacontroller import HoribaController

    return HoribaController(
        enable_logging=False,
        enable_rotation_stage=False,
        enable_thorlabs_stage=False,
    )


@pytest.fixture
async def connected_controller(controller):
    """Convenience: a HoribaController past connect_hardware()."""
    await controller.connect_hardware()
    return controller
