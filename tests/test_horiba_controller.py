"""Behavioural tests for HoribaController."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


def _run(coro):
    return asyncio.run(coro)


# ── grating calibration is never touched on connect ───────────────────


@pytest.mark.parametrize("already_initialized", [False, True])
def test_connect_never_calibrates_on_startup(
    mock_horiba_sdk, controller, monkeypatch, already_initialized
):
    """Basic acquisition must always work: connect_hardware must NEVER call
    mono_setPosition (calibrate_wavelength), even with a saved offset and a
    fresh home. Feeding a post-home read into mono_setPosition was what
    misaligned the spectrometer on startup — reapply is now manual-only."""
    from unittest.mock import AsyncMock
    import grating_calib

    mono = mock_horiba_sdk.mono
    mono.is_initialized = AsyncMock(return_value=already_initialized)
    mono.get_current_wavelength = AsyncMock(return_value=500.0)
    mono.calibrate_wavelength = AsyncMock()
    monkeypatch.setattr(grating_calib, "load_saved_offset", lambda: 0.3)

    _run(controller.connect_hardware())

    mono.calibrate_wavelength.assert_not_awaited()


# ── manual reapply (reapply_saved_calibration) ─────────────────────────


def test_reapply_saved_calibration_returns_none_without_offset(
    mock_horiba_sdk, controller, monkeypatch
):
    """With nothing saved, the manual reapply is a no-op returning None and
    never touches the wavelength frame."""
    import grating_calib

    mono = mock_horiba_sdk.mono
    mono.calibrate_wavelength = AsyncMock()
    monkeypatch.setattr(grating_calib, "load_saved_offset", lambda: None)

    async def main():
        await controller.connect_hardware()
        return await controller.reapply_saved_calibration()

    result = _run(main())

    assert result is None
    mono.calibrate_wavelength.assert_not_awaited()


def test_reapply_saved_calibration_applies_offset_once(
    mock_horiba_sdk, controller, monkeypatch
):
    """Manual reapply shifts the current frame by the saved offset via a
    single mono_setPosition (500.0 + 0.3 -> 500.3), invalidates the cached
    wavelength, and returns the new value."""
    import grating_calib

    mono = mock_horiba_sdk.mono
    mono.get_current_wavelength = AsyncMock(return_value=500.0)
    mono.calibrate_wavelength = AsyncMock()
    monkeypatch.setattr(grating_calib, "load_saved_offset", lambda: 0.3)

    async def main():
        await controller.connect_hardware()
        controller._current_params["wavelength"] = 700.0  # stale cache
        return await controller.reapply_saved_calibration()

    result = _run(main())

    mono.calibrate_wavelength.assert_awaited_once()
    (applied,) = mono.calibrate_wavelength.await_args.args
    assert abs(applied - 500.3) < 1e-9
    assert abs(result - 500.3) < 1e-9
    # Cache invalidated so the next scan re-issues a move against the new frame.
    assert controller._current_params["wavelength"] is None


# ── _sdk_lock serialization (commit 3) ────────────────────────────────


def test_sdk_lock_serializes_overlapping_acquires(mock_horiba_sdk, controller):
    """Two concurrent acquire_spectrum calls must not interleave their
    ICL command sequences. The single shared asyncio.Lock guarantees
    that ``acquisition_start`` always pairs with the matching
    ``get_acquisition_data`` before another start fires.
    """
    call_log: list[str] = []

    ccd = mock_horiba_sdk.ccd

    async def logging_acq_start(*_args, **_kwargs):
        call_log.append("start")
        # Yield once so the other coroutine has a chance to barge in
        # if the lock is missing.
        await asyncio.sleep(0)

    async def logging_get_data():
        call_log.append("data")
        await asyncio.sleep(0)
        return [{"roi": [{"xData": [1, 2], "yData": [3, 4]}]}]

    ccd.acquisition_start.side_effect = logging_acq_start
    ccd.get_acquisition_data.side_effect = logging_get_data

    async def main():
        await controller.connect_hardware()
        await asyncio.gather(
            controller.acquire_spectrum(),
            controller.acquire_spectrum(),
        )

    _run(main())

    # Expect strictly start→data→start→data, never start→start→data→data.
    assert call_log == ["start", "data", "start", "data"], call_log


# ── acquisition_abort wrapper (commit 3) ──────────────────────────────


def test_acquisition_abort_calls_ccd_and_waits_for_idle(mock_horiba_sdk, controller):
    """acquisition_abort must call ccd.acquisition_abort and then poll
    get_acquisition_busy until it goes False before returning.
    """

    async def main():
        # connect_hardware itself polls get_acquisition_busy via
        # _wait_for_ccd, so install the side_effect AFTER connect.
        await controller.connect_hardware()
        busy_returns = [True, True, False]
        mock_horiba_sdk.ccd.get_acquisition_busy.side_effect = lambda: busy_returns.pop(
            0
        )
        await controller.acquisition_abort()
        return busy_returns

    remaining = _run(main())

    mock_horiba_sdk.ccd.acquisition_abort.assert_awaited()
    # Polled exactly three times: True, True, False — then returned.
    assert remaining == []


def test_acquisition_abort_noop_when_disconnected(mock_horiba_sdk, controller):
    """Aborting on a disconnected controller should be safe and silent."""

    async def main():
        # Never call connect_hardware.
        await controller.acquisition_abort()

    _run(main())

    mock_horiba_sdk.ccd.acquisition_abort.assert_not_awaited()


# ── re-raise on acquire failure (commit 4) ────────────────────────────


def test_acquire_spectrum_reraises_on_ccd_error(mock_horiba_sdk, controller):
    """Errors inside acquire_spectrum must propagate — silently
    returning None caused callers to crash with a confusing TypeError
    in zip(x_data, y_data)."""

    async def boom():
        raise RuntimeError("ICL went away")

    mock_horiba_sdk.ccd.get_acquisition_data.side_effect = boom

    async def main():
        await controller.connect_hardware()
        await controller.acquire_spectrum()

    with pytest.raises(RuntimeError, match="ICL went away"):
        _run(main())

    # And the controller must mark itself disconnected so the next
    # call triggers a fresh connect_hardware.
    assert controller.is_connected is False


def test_acquire_spectrum_clears_acquiring_flag_on_error(mock_horiba_sdk, controller):
    """A failed acquisition must not leave _acquiring stuck True,
    or temperature polls would silently return -999.0 forever."""

    async def boom():
        raise RuntimeError("nope")

    mock_horiba_sdk.ccd.get_acquisition_data.side_effect = boom

    async def main():
        await controller.connect_hardware()
        try:
            await controller.acquire_spectrum()
        except RuntimeError:
            pass

    _run(main())
    assert controller._acquiring is False


# ── acquire_image (commit 5) ──────────────────────────────────────────


def _image_payload(y_size: int, x_size: int):
    """ICL acquisition payload for an image: yData is a 1-D row-major
    list of length y_size*x_size."""
    flat = list(range(y_size * x_size))
    return [{"roi": [{"xData": [], "yData": flat}]}]


def test_acquire_image_full_chip_returns_2d(mock_horiba_sdk, controller):
    """Full-chip image acquire returns shape (chip_y, chip_x) using
    the dimensions reported by get_configuration."""
    import numpy as np

    cfg = {"chipWidth": 1024, "chipHeight": 256}
    mock_horiba_sdk.ccd.get_configuration.return_value = cfg
    mock_horiba_sdk.ccd.get_acquisition_data.side_effect = lambda: _image_payload(
        cfg["chipHeight"], cfg["chipWidth"]
    )

    async def main():
        await controller.connect_hardware()
        return await controller.acquire_image()

    arr = _run(main())
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (256, 1024)


def test_acquire_image_custom_roi_shape(mock_horiba_sdk, controller):
    """Caller-supplied ROI must be honoured in the reshape."""
    import numpy as np

    mock_horiba_sdk.ccd.get_configuration.return_value = {
        "chipWidth": 1024,
        "chipHeight": 256,
    }
    mock_horiba_sdk.ccd.get_acquisition_data.side_effect = lambda: _image_payload(
        64, 128
    )

    async def main():
        await controller.connect_hardware()
        return await controller.acquire_image(
            x_origin=0, y_origin=0, x_size=128, y_size=64, x_bin=1, y_bin=1
        )

    arr = _run(main())
    assert arr.shape == (64, 128)


def test_acquire_image_uses_image_format(mock_horiba_sdk, controller):
    """Must call set_acquisition_format with AcquisitionFormat.IMAGE,
    not SPECTRA."""
    from horiba_sdk.core.acquisition_format import AcquisitionFormat

    mock_horiba_sdk.ccd.get_configuration.return_value = {
        "chipWidth": 4,
        "chipHeight": 2,
    }
    mock_horiba_sdk.ccd.get_acquisition_data.side_effect = lambda: _image_payload(2, 4)

    async def main():
        await controller.connect_hardware()
        await controller.acquire_image()

    _run(main())

    calls = mock_horiba_sdk.ccd.set_acquisition_format.await_args_list
    formats = [c.args[1] for c in calls]
    assert AcquisitionFormat.IMAGE in formats


# ── Stage handling across the spectrometer-only shutdown (commit 8b) ──


def test_shutdown_spectrometer_does_not_disconnect_stages(mock_horiba_sdk, monkeypatch):
    """A spectrometer-only shutdown must leave the rotation stages
    connected. The full shutdown() (called only on app close) is what
    tears them down."""
    from unittest.mock import MagicMock
    import horibacontroller

    fake_stage_cls = MagicMock(name="OptoSigmaCls")
    fake_stage_instance = MagicMock(name="OptoSigmaInst")
    fake_stage_instance.connect.return_value = True
    fake_stage_instance.is_connected = True
    fake_stage_instance.degree = 47.5
    fake_stage_cls.return_value = fake_stage_instance

    monkeypatch.setattr(horibacontroller, "OptoSigmaController", fake_stage_cls)

    c = horibacontroller.HoribaController(
        enable_logging=False,
        enable_rotation_stage=True,
        enable_thorlabs_stage=False,
    )

    async def main():
        await c.connect_hardware()
        await c.shutdown_spectrometer()

    _run(main())

    fake_stage_instance.disconnect.assert_not_called()
    assert c.rotation_stage is fake_stage_instance


def test_full_shutdown_does_disconnect_stages(mock_horiba_sdk, monkeypatch):
    """The all-up shutdown() (closeEvent path) does tear down the
    rotation stage by design."""
    from unittest.mock import MagicMock
    import horibacontroller

    fake_stage_cls = MagicMock(name="OptoSigmaCls")
    fake_stage_instance = MagicMock(name="OptoSigmaInst")
    fake_stage_instance.connect.return_value = True
    fake_stage_instance.is_connected = True
    fake_stage_instance.degree = 0.0
    fake_stage_cls.return_value = fake_stage_instance

    monkeypatch.setattr(horibacontroller, "OptoSigmaController", fake_stage_cls)

    c = horibacontroller.HoribaController(
        enable_logging=False,
        enable_rotation_stage=True,
        enable_thorlabs_stage=False,
    )

    async def main():
        await c.connect_hardware()
        await c.shutdown()

    _run(main())

    fake_stage_instance.disconnect.assert_called_once()


def test_connect_hardware_reconnects_disconnected_stage(mock_horiba_sdk, monkeypatch):
    """If the rotation stage went disconnected for any reason,
    connect_hardware must bring it back and refresh last_angle."""
    from unittest.mock import MagicMock
    import horibacontroller

    fake_stage_cls = MagicMock(name="OptoSigmaCls")
    fake_stage_instance = MagicMock(name="OptoSigmaInst")
    fake_stage_instance.connect.return_value = True
    fake_stage_instance.is_connected = True
    fake_stage_instance.degree = 12.5
    fake_stage_cls.return_value = fake_stage_instance

    monkeypatch.setattr(horibacontroller, "OptoSigmaController", fake_stage_cls)

    c = horibacontroller.HoribaController(
        enable_logging=False,
        enable_rotation_stage=True,
        enable_thorlabs_stage=False,
    )

    # Simulate the stage having been disconnected mid-session.
    fake_stage_instance.is_connected = False
    fake_stage_instance.degree = 99.0  # the new physical position

    # Bring spectrometer up. connect_hardware must drive the stage
    # back up and refresh last_angle.
    fake_stage_instance.reconnect = MagicMock(return_value=True)

    # When reconnect succeeds, the stage reports its current angle.
    def reconnect_side():
        fake_stage_instance.is_connected = True
        return True

    fake_stage_instance.reconnect.side_effect = reconnect_side

    async def main():
        await c.connect_hardware()

    _run(main())

    fake_stage_instance.reconnect.assert_called_once()
    assert c.last_angle == 99.0


def test_acquire_image_does_not_mutate_payload(mock_horiba_sdk, controller):
    """The returned ndarray must be a copy, not a view into the
    list payload returned by the SDK."""
    import numpy as np

    payload_holder = {}

    def _payload():
        flat = [1.0, 2.0, 3.0, 4.0]
        payload_holder["last"] = flat
        return [{"roi": [{"xData": [], "yData": flat}]}]

    mock_horiba_sdk.ccd.get_configuration.return_value = {
        "chipWidth": 2,
        "chipHeight": 2,
    }
    mock_horiba_sdk.ccd.get_acquisition_data.side_effect = _payload

    async def main():
        await controller.connect_hardware()
        return await controller.acquire_image()

    arr = _run(main())
    arr[0, 0] = 999.0
    assert payload_holder["last"][0] == 1.0, (
        "acquire_image must not return a view that mutates the SDK payload"
    )
