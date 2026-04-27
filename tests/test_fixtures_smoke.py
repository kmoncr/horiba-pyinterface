"""Smoke tests for the conftest fixtures.

These only check that the mock SDK plumbing works; controller behaviour
gets exercised in dedicated tests once features land in later commits.
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def test_mock_horiba_sdk_exposes_ccd_and_mono(mock_horiba_sdk):
    assert mock_horiba_sdk.mono is not None
    assert mock_horiba_sdk.ccd is not None


def test_controller_constructs_with_mock_sdk(controller):
    assert controller is not None
    assert controller.is_connected is False


def test_connect_hardware_uses_mock_dm(mock_horiba_sdk, controller):
    # connect_hardware must succeed without touching real hardware.
    asyncio.run(controller.connect_hardware())
    assert controller.is_connected is True
    assert controller.mono is mock_horiba_sdk.mono
    assert controller.ccd is mock_horiba_sdk.ccd


def test_qsettings_isolated_to_tmp(tmp_path):
    """QSettings writes resolve under the test's tmp_path, not the registry.

    Production code constructs QSettings with explicit IniFormat/UserScope
    (so the autouse setPath fixture can redirect it on every platform).
    The smoke test mirrors that constructor.
    """
    from PyQt5.QtCore import QSettings

    s = QSettings(QSettings.IniFormat, QSettings.UserScope,
                  "HoribaIHR550Test", "Smoke")
    s.setValue("hello", "world")
    s.sync()

    fname = Path(s.fileName())
    assert tmp_path in fname.parents, (
        f"expected QSettings file under {tmp_path}, got {fname!r}"
    )
    # And the value round-trips through this isolated store.
    s2 = QSettings(QSettings.IniFormat, QSettings.UserScope,
                   "HoribaIHR550Test", "Smoke")
    assert s2.value("hello") == "world"
