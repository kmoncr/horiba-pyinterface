"""Behavioural tests for HoribaController."""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


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
        mock_horiba_sdk.ccd.get_acquisition_busy.side_effect = (
            lambda: busy_returns.pop(0)
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
