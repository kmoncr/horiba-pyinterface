"""Image-mode acquisition window for the Horiba Synapse Plus CCD.

Replaces the previous one-shot script. ImageWindow accepts an
already-running HoribaController and asyncio loop so it can be opened
in-process from the main GUI without tearing down the ICL connection.

When invoked standalone (``python image.py``) the constructor builds
its own controller and background loop, preserving direct script use.
"""

from __future__ import annotations

import asyncio
import functools
import sys
import threading
from typing import Optional

import numpy as np
import pyqtgraph as pg
from loguru import logger

from PyQt5 import QtCore
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QComboBox,
)

# Match the existing image.py behaviour of disabling websocket size /
# ping limits — large image frames can exceed the default frame size.
import websockets
websockets.connect = functools.partial(
    websockets.connect, max_size=None, ping_interval=None
)

from horibaprocedure import GAIN_CHOICES, SPEED_CHOICES, PARAM_MAP

try:
    from horibacontroller import HoribaController
except ImportError:
    HoribaController = None  # type: ignore


class ImageWindow(QMainWindow):
    """A QMainWindow that drives a single 2D image acquisition.

    Parameters
    ----------
    controller :
        An already-built HoribaController. If None, a new one is
        constructed and connected on a fresh background event loop.
    loop :
        The asyncio loop driving ``controller``. Required when
        ``controller`` is provided.
    """

    image_ready = QtCore.pyqtSignal(object)  # 2-D ndarray
    acquire_error = QtCore.pyqtSignal(str)

    def __init__(self,
                 controller: 'HoribaController | None' = None,
                 loop: 'asyncio.AbstractEventLoop | None' = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Horiba Image Mode")
        self.setMinimumSize(900, 600)

        if controller is not None and loop is not None:
            self.controller = controller
            self.loop = loop
            self.loop_thread = None
            self._owns_controller = False
        else:
            if HoribaController is None:
                raise RuntimeError("HoribaController is unavailable; "
                                   "cannot run ImageWindow standalone.")
            self.controller = HoribaController(enable_logging=True)
            self.loop = asyncio.new_event_loop()
            self.loop_thread = threading.Thread(
                target=self._run_loop, args=(self.loop,), daemon=True
            )
            self.loop_thread.start()
            self._owns_controller = True
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self.controller.connect_hardware(), self.loop
                )
                fut.result(timeout=60)
            except Exception as e:
                logger.error(f"connect_hardware failed in standalone ImageWindow: {e}")

        self._build_ui()
        self.image_ready.connect(self._on_image_ready)
        self.acquire_error.connect(self._on_acquire_error)
        self._acquiring_in_flight = False

    # ── UI ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # Parameter strip (acquisition + ROI)
        params_box = QGroupBox("Image Parameters")
        params_layout = QHBoxLayout(params_box)

        # Exposure
        exp_form = QFormLayout()
        self.exposure_spin = QDoubleSpinBox()
        self.exposure_spin.setRange(0.001, 600.0)
        self.exposure_spin.setDecimals(3)
        self.exposure_spin.setValue(1.0)
        self.exposure_spin.setSuffix(" s")
        exp_form.addRow("Exposure:", self.exposure_spin)

        self.gain_combo = QComboBox()
        self.gain_combo.addItems(GAIN_CHOICES.keys())
        self.gain_combo.setCurrentText('Best Dynamic Range')
        exp_form.addRow("Gain:", self.gain_combo)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(SPEED_CHOICES.keys())
        self.speed_combo.setCurrentText('50 kHz')
        exp_form.addRow("Speed:", self.speed_combo)

        params_layout.addLayout(exp_form)

        # ROI block
        roi_form = QFormLayout()
        self.roi_mode_combo = QComboBox()
        self.roi_mode_combo.addItems(["Full chip", "Custom"])
        self.roi_mode_combo.currentTextChanged.connect(self._on_roi_mode_changed)
        roi_form.addRow("ROI:", self.roi_mode_combo)

        self.x_origin_spin = QSpinBox(); self.x_origin_spin.setRange(0, 4096)
        self.y_origin_spin = QSpinBox(); self.y_origin_spin.setRange(0, 4096)
        self.x_size_spin   = QSpinBox(); self.x_size_spin.setRange(1, 4096)
        self.y_size_spin   = QSpinBox(); self.y_size_spin.setRange(1, 4096)
        # Reasonable defaults for the Synapse Plus chip; the controller
        # overrides them with the real chip dimensions when running.
        self.x_size_spin.setValue(1024)
        self.y_size_spin.setValue(256)
        roi_form.addRow("x origin:", self.x_origin_spin)
        roi_form.addRow("y origin:", self.y_origin_spin)
        roi_form.addRow("x size:",   self.x_size_spin)
        roi_form.addRow("y size:",   self.y_size_spin)
        self._on_roi_mode_changed("Full chip")

        params_layout.addLayout(roi_form)

        # Action button
        action_box = QVBoxLayout()
        self.acquire_button = QPushButton("Acquire")
        self.acquire_button.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold;"
        )
        self.acquire_button.setMinimumHeight(40)
        self.acquire_button.clicked.connect(self._on_acquire_clicked)
        self.status_label = QLabel("Ready")
        action_box.addWidget(self.acquire_button)
        action_box.addWidget(self.status_label)
        action_box.addStretch()
        params_layout.addLayout(action_box)

        outer.addWidget(params_box)

        # Image display
        self.image_view = pg.ImageView()
        try:
            self.image_view.setColorMap(pg.colormap.get('viridis'))
        except Exception:
            # pyqtgraph older than 0.12.4 may not have pg.colormap.get;
            # fall back silently.
            pass
        outer.addWidget(self.image_view, stretch=1)

    def _on_roi_mode_changed(self, text: str) -> None:
        custom = (text == "Custom")
        for w in (self.x_origin_spin, self.y_origin_spin,
                  self.x_size_spin, self.y_size_spin):
            w.setEnabled(custom)

    # ── Acquire flow ──────────────────────────────────────────────────

    def _enumconv(self, param_name: str, value: str):
        d = PARAM_MAP.get(param_name)
        if d and value in d:
            return d[value].value
        return None

    def _on_acquire_clicked(self) -> None:
        if self._acquiring_in_flight:
            return
        self._acquiring_in_flight = True
        self.acquire_button.setEnabled(False)
        self.status_label.setText("Acquiring…")

        kwargs = {
            "exposure": self.exposure_spin.value(),
            "gain":  self._enumconv("gain", self.gain_combo.currentText()),
            "speed": self._enumconv("speed", self.speed_combo.currentText()),
        }
        if self.roi_mode_combo.currentText() == "Custom":
            kwargs.update(
                x_origin=self.x_origin_spin.value(),
                y_origin=self.y_origin_spin.value(),
                x_size=self.x_size_spin.value(),
                y_size=self.y_size_spin.value(),
                x_bin=1, y_bin=1,
            )

        future = asyncio.run_coroutine_threadsafe(
            self.controller.acquire_image(**kwargs), self.loop
        )
        future.add_done_callback(self._on_acquire_done)

    def _on_acquire_done(self, fut) -> None:
        try:
            arr = fut.result()
        except Exception as e:
            logger.exception("image acquisition failed")
            self.acquire_error.emit(str(e))
            return
        if arr is None or not isinstance(arr, np.ndarray):
            self.acquire_error.emit("acquire_image returned no data")
            return
        self.image_ready.emit(arr)

    @QtCore.pyqtSlot(object)
    def _on_image_ready(self, arr) -> None:
        self.image_view.setImage(arr, autoLevels=True)
        self.status_label.setText(
            f"Done · {arr.shape[1]}×{arr.shape[0]} px"
        )
        self._acquiring_in_flight = False
        self.acquire_button.setEnabled(True)

    @QtCore.pyqtSlot(str)
    def _on_acquire_error(self, msg: str) -> None:
        self.status_label.setText(f"Error: {msg}")
        self._acquiring_in_flight = False
        self.acquire_button.setEnabled(True)

    # ── Lifecycle ─────────────────────────────────────────────────────

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def closeEvent(self, event):
        # Always cancel any in-flight CCD acquisition.
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.controller.acquisition_abort(), self.loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.warning(f"acquisition_abort during close failed: {e}")

        if self._owns_controller:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.controller.shutdown(), self.loop
                )
                future.result(timeout=5)
            except Exception as e:
                logger.error(f"shutdown failed: {e}")
            finally:
                if self.loop and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.loop.stop)
                    if self.loop_thread:
                        self.loop_thread.join(timeout=2)

        event.accept()


def main_standalone() -> int:
    """Entry point for ``python image.py``."""
    from logging_setup import setup_file_logging
    log_path = setup_file_logging("image")
    logger.info(f"Logging to {log_path}")
    print(f"[image] log file: {log_path}", flush=True)

    app = QApplication(sys.argv)
    win = ImageWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main_standalone())
