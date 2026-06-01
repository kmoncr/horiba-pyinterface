"""Grating-zero calibration tool.

Provides a small in-process window for applying the EzSpec
``mono_setPosition`` calibration (Python: ``mono.calibrate_wavelength``)
which shifts the reported-wavelength frame so a known laser line lands
at the expected wavelength / 0 cm⁻¹. The SDK forgets the calibration on
``mono_init``, so the last applied value is persisted to
``grating_calib.json`` and reapplied by HoribaController.connect_hardware.
"""

import asyncio
import json
import pathlib

from loguru import logger
from PyQt5 import QtCore, QtWidgets
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QDoubleSpinBox,
)


CALIB_FILE = pathlib.Path(__file__).parent / "grating_calib.json"


def load_saved_calibration() -> float | None:
    """Return the last applied calibration in nm, or None if none saved."""
    try:
        if not CALIB_FILE.exists():
            return None
        data = json.loads(CALIB_FILE.read_text())
        v = data.get("last_calibration_nm")
        return float(v) if v is not None else None
    except Exception as e:
        logger.warning(f"could not read {CALIB_FILE}: {e}")
        return None


def save_calibration(nm: float) -> None:
    CALIB_FILE.write_text(json.dumps({"last_calibration_nm": nm}))


def forget_calibration() -> None:
    if CALIB_FILE.exists():
        CALIB_FILE.unlink()


class GratingCalibrationWindow(QWidget):
    wavelength_read = QtCore.pyqtSignal(float)
    op_finished = QtCore.pyqtSignal(str, bool, str)  # (op_name, success, message)

    def __init__(self, controller=None, loop: 'asyncio.AbstractEventLoop | None' = None,
                 parent=None):
        super().__init__(parent, QtCore.Qt.Window)
        self.controller = controller
        self.loop = loop
        self._busy = False

        self.setWindowTitle("Grating Calibration")
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        # ── Status row ────────────────────────────────────────────────
        status_row = QHBoxLayout()
        self.current_label = QLabel("Current wavelength: --.--- nm")
        self.current_label.setStyleSheet("font-weight: bold;")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setFixedWidth(80)
        self.refresh_button.clicked.connect(self.refresh_current)
        status_row.addWidget(self.current_label, stretch=1)
        status_row.addWidget(self.refresh_button)
        root.addLayout(status_row)

        # ── Move group ────────────────────────────────────────────────
        move_group = QGroupBox("1. Move grating to a reference line")
        move_layout = QHBoxLayout(move_group)
        self.move_input = QDoubleSpinBox()
        self.move_input.setRange(200.0, 2000.0)
        self.move_input.setDecimals(3)
        self.move_input.setSuffix(" nm")
        self.move_input.setValue(532.0)
        self.move_button = QPushButton("Move")
        self.move_button.clicked.connect(self.do_move)
        move_layout.addWidget(QLabel("Go to:"))
        move_layout.addWidget(self.move_input, stretch=1)
        move_layout.addWidget(self.move_button)
        root.addWidget(move_group)

        # ── Calibrate group ───────────────────────────────────────────
        calib_group = QGroupBox("2. Calibrate current position")
        calib_tip = (
            "Enter the actual wavelength of the line currently under the "
            "grating. The reported wavelength scale shifts so this position "
            "reads that value — the grating does not physically move."
        )
        calib_group.setToolTip(calib_tip)
        calib_layout = QHBoxLayout(calib_group)
        self.calib_input = QDoubleSpinBox()
        self.calib_input.setRange(200.0, 2000.0)
        self.calib_input.setDecimals(3)
        self.calib_input.setSuffix(" nm")
        self.calib_input.setValue(532.0)
        self.calib_input.setToolTip(calib_tip)
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self.do_calibrate)
        calib_layout.addWidget(QLabel("True wavelength:"))
        calib_layout.addWidget(self.calib_input, stretch=1)
        calib_layout.addWidget(self.apply_button)
        root.addWidget(calib_group)

        # ── Persistence row ───────────────────────────────────────────
        persist_row = QHBoxLayout()
        self.saved_label = QLabel()
        # The footer note now lives here as a tooltip to keep the window clean.
        self.saved_label.setToolTip(
            "Calibration is held by the SDK only and is wiped when the "
            "monochromator is re-initialized. The saved value is "
            "auto-reapplied on next connect."
        )
        self.forget_button = QPushButton("Forget saved")
        self.forget_button.setFixedWidth(110)
        self.forget_button.clicked.connect(self.do_forget)
        persist_row.addWidget(self.saved_label, stretch=1)
        persist_row.addWidget(self.forget_button)
        root.addLayout(persist_row)
        self._refresh_saved_label()

        # ── Status line ───────────────────────────────────────────────
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #555; font-size: 11px;")
        root.addWidget(self.status_label)

        # Signal wiring
        self.wavelength_read.connect(self._on_wavelength_read)
        self.op_finished.connect(self._on_op_finished)

        # If the controller is already connected, prefill the display.
        if self.controller is not None and getattr(self.controller, "is_connected", False):
            self.refresh_current()

    # ── Dispatch helpers ──────────────────────────────────────────────

    def _can_dispatch(self) -> bool:
        if self.controller is None or self.loop is None:
            logger.warning("Grating Calib: controller/loop not available")
            return False
        if not getattr(self.controller, "is_connected", False):
            logger.warning("Grating Calib: hardware not connected")
            return False
        if self._busy:
            return False
        return True

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for w in (self.move_button, self.apply_button, self.refresh_button):
            w.setEnabled(not busy)

    # ── Refresh current wavelength ────────────────────────────────────

    def refresh_current(self):
        if not self._can_dispatch():
            return
        self.status_label.setText("Reading current wavelength…")
        self._set_busy(True)
        fut = asyncio.run_coroutine_threadsafe(
            self.controller.get_current_wavelength(), self.loop
        )
        fut.add_done_callback(self._refresh_cb)

    def _refresh_cb(self, fut):
        try:
            nm = float(fut.result())
            self.wavelength_read.emit(nm)
            self.op_finished.emit("refresh", True, "")
        except Exception as e:
            self.op_finished.emit("refresh", False, str(e))

    @QtCore.pyqtSlot(float)
    def _on_wavelength_read(self, nm: float):
        self.current_label.setText(f"Current wavelength: {nm:.3f} nm")

    # ── Move grating ──────────────────────────────────────────────────

    def do_move(self):
        if not self._can_dispatch():
            return
        target = self.move_input.value()
        logger.info(f"Grating Calib: moving to {target:.3f} nm")
        self.status_label.setText(f"Moving to {target:.3f} nm…")
        self._set_busy(True)
        fut = asyncio.run_coroutine_threadsafe(
            self._move_then_read(target), self.loop
        )
        fut.add_done_callback(self._move_cb)

    async def _move_then_read(self, target: float) -> float:
        await self.controller.move_to_wavelength(target)
        return float(await self.controller.get_current_wavelength())

    def _move_cb(self, fut):
        try:
            nm = float(fut.result())
            self.wavelength_read.emit(nm)
            self.op_finished.emit("move", True, "")
        except Exception as e:
            self.op_finished.emit("move", False, str(e))

    # ── Apply calibration ─────────────────────────────────────────────

    def do_calibrate(self):
        if not self._can_dispatch():
            return
        target = self.calib_input.value()
        logger.info(f"Grating Calib: applying calibration {target:.3f} nm")
        self.status_label.setText(f"Applying calibration {target:.3f} nm…")
        self._set_busy(True)
        fut = asyncio.run_coroutine_threadsafe(
            self._calibrate_then_read(target), self.loop
        )
        fut.add_done_callback(lambda f, t=target: self._calibrate_cb(f, t))

    async def _calibrate_then_read(self, target: float) -> float:
        await self.controller.calibrate_wavelength(target)
        return float(await self.controller.get_current_wavelength())

    def _calibrate_cb(self, fut, target: float):
        try:
            nm = float(fut.result())
            save_calibration(target)
            self.wavelength_read.emit(nm)
            self.op_finished.emit("calibrate", True, f"{target:.3f}")
        except Exception as e:
            self.op_finished.emit("calibrate", False, str(e))

    # ── Forget saved ──────────────────────────────────────────────────

    def do_forget(self):
        forget_calibration()
        self._refresh_saved_label()
        logger.info("Grating Calib: forgot saved value")

    def _refresh_saved_label(self):
        saved = load_saved_calibration()
        if saved is None:
            self.saved_label.setText("Saved calibration: none")
            self.forget_button.setEnabled(False)
        else:
            self.saved_label.setText(
                f"Saved calibration: {saved:.3f} nm (auto-applied on connect)"
            )
            self.forget_button.setEnabled(True)

    # ── Op completion ─────────────────────────────────────────────────

    @QtCore.pyqtSlot(str, bool, str)
    def _on_op_finished(self, op_name: str, success: bool, message: str):
        self._set_busy(False)
        if not success:
            logger.error(f"Grating Calib: {op_name} failed: {message}")
            self.status_label.setText(f"{op_name.capitalize()} failed: {message}")
            return
        if op_name == "calibrate":
            self._refresh_saved_label()
            logger.success(f"Grating Calib: applied {message} nm")
            self.status_label.setText(f"Applied calibration {message} nm ✓")
        elif op_name == "move":
            self.status_label.setText("Move complete ✓")
        else:  # refresh
            self.status_label.setText("Ready")
