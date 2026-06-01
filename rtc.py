import sys
import os
import asyncio
import threading
import time
from loguru import logger

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QPushButton, QDoubleSpinBox, QComboBox, QSpinBox,
    QCheckBox, QStackedWidget, QLabel,
)
import pyqtgraph as pg
import numpy as np

# Make pyqtgraph's image widgets interpret arrays the way numpy does:
# arr[row, col] = arr[y, x]. Without this, ImageView treats axis 0 as
# X and a (256, 1024) array displays 256 wide × 1024 tall — opposite
# of the actual chip orientation.
pg.setConfigOptions(imageAxisOrder='row-major')

try:
    from horibacontroller import HoribaController
    from horibaprocedure import (
        HoribaSpectrumProcedure, 
        GRATING_CHOICES, GAIN_CHOICES, SPEED_CHOICES, 
        PARAM_MAP, GratingEnum
    )
except ImportError:
    print("could not import 'horibacontroller.py' or 'horibaprocedure.py'. place rtc.py in the same directory as these files.")
    sys.exit(1)


class LiveViewWindow(QWidget):
    data_ready = QtCore.pyqtSignal(object, object)  # (x_data, y_data)
    image_ready = QtCore.pyqtSignal(object)         # 2-D ndarray
    scan_error = QtCore.pyqtSignal(str)
    scanning_changed = QtCore.pyqtSignal(bool)
    connection_changed = QtCore.pyqtSignal(bool, str)
    temp_updated = QtCore.pyqtSignal(float)
    angle_updated = QtCore.pyqtSignal(float)

    def __init__(self, controller: 'HoribaController | None' = None,
                 loop: 'asyncio.AbstractEventLoop | None' = None,
                 parent=None):
        # Pass Qt.Window so the widget always opens as a top-level
        # window even when a parent is set. Without this flag, a
        # parented QWidget gets embedded inside the parent and shows
        # up overlaying the main GUI.
        super().__init__(parent, QtCore.Qt.Window)

        # Constructor injection lets the main GUI share its controller
        # and event loop, eliminating the slow ICL teardown/restart
        # that was happening when RTC ran as a separate subprocess.
        # When invoked standalone (python rtc.py), build our own.
        if controller is not None and loop is not None:
            self.controller = controller
            self.loop = loop
            self.loop_thread = None
            self._owns_controller = False
        else:
            self.controller = HoribaController(enable_logging=True)
            self.loop = None
            self.loop_thread = None
            self._start_event_loop()
            self._owns_controller = True

            logger.info("starting hardware connection...")
            try:
                self.run_async_task(self.controller.connect_hardware(), timeout=60)
            except Exception as e:
                logger.error(f"Failed to initialize hardware on startup: {e}")

        self.worker_thread = None
        self.stop_event = threading.Event()
        self.is_scanning = False
        self._hw_ready = self.controller.is_connected
        self._temp_pending = False

        self.latest_wavelength = None
        self.latest_intensity = None
        
        self.setWindowTitle("Horiba RTC")
        self.setGeometry(100, 100, 1200, 700)
        
        main_layout = QHBoxLayout()
        
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(15)
        
        # The display swaps between a 1-D spectrum plot and a 2-D
        # image view depending on the mode combo (Spectrum / Image).
        plot_widget = QWidget()
        plot_layout = QVBoxLayout()
        self.plot_stack = QStackedWidget()

        self.plot_widget = pg.PlotWidget()
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.setLabels(left='Intensity (counts)', bottom='Wavelength (nm)')
        self.plot_data_item = self.plot_item.plot(pen='y')
        self.plot_stack.addWidget(self.plot_widget)   # index 0 = Spectrum

        self.image_view = pg.ImageView()
        try:
            self.image_view.setColorMap(pg.colormap.get('viridis'))
        except Exception:
            pass
        self.plot_stack.addWidget(self.image_view)    # index 1 = Image

        plot_layout.addWidget(self.plot_stack)
        plot_widget.setLayout(plot_layout)
    
        scan_box = QGroupBox("Scan Control")
        scan_outer_layout = QVBoxLayout()

        scan_layout = QHBoxLayout()
        self.start_button = QPushButton("START")
        self.start_button.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        self.start_button.clicked.connect(self.start_scan)
        self.start_button.setEnabled(False)  # enabled once hardware connects

        self.stop_button = QPushButton("STOP")
        self.stop_button.setStyleSheet("background-color: #f44336; color: white; font-weight: bold;")
        self.stop_button.clicked.connect(self.stop_scan)
        self.stop_button.setEnabled(False)

        scan_layout.addWidget(self.start_button)
        scan_layout.addWidget(self.stop_button)
        scan_outer_layout.addLayout(scan_layout)

        self.status_label = QLabel("Status: connecting…")
        self.status_label.setStyleSheet("color: orange; font-weight: bold;")
        self.temp_label = QLabel("CCD Temp: -- °C")
        self.temp_label.setStyleSheet("font-weight: bold;")
        scan_outer_layout.addWidget(self.status_label)
        scan_outer_layout.addWidget(self.temp_label)

        scan_box.setLayout(scan_outer_layout)
        controls_layout.addWidget(scan_box)

        # Acquisition-mode toggle (1-D spectrum vs 2-D image preview).
        mode_box = QGroupBox("Acquisition Mode")
        mode_form = QFormLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Spectrum", "Image"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        mode_form.addRow("Mode:", self.mode_combo)
        mode_box.setLayout(mode_form)
        controls_layout.addWidget(mode_box)

        plot_options_box = QGroupBox("Plot Options")
        plot_options_layout = QFormLayout()

        self.x_axis_combo = QComboBox()
        self.x_axis_combo.addItems([
            "Wavelength (nm)",
            "Raman shift (cm⁻¹)",
            "Energy (eV)",
            "Raman shift (eV)",
        ])
        self.x_axis_combo.currentTextChanged.connect(self._on_x_axis_changed)
        plot_options_layout.addRow("X axis:", self.x_axis_combo)

        # Auto-scale toggle. When unchecked, the user's pan/zoom is
        # preserved across new frames; when re-checked, the next frame
        # autoscales again.
        self.autoscale_button = QPushButton("Auto Scale")
        self.autoscale_button.setCheckable(True)
        self.autoscale_button.setChecked(True)
        self.autoscale_button.toggled.connect(self._on_autoscale_toggled)
        plot_options_layout.addRow(self.autoscale_button)

        plot_options_box.setLayout(plot_options_layout)
        controls_layout.addWidget(plot_options_box)

        spec_box = QGroupBox("Spectrometer Parameters")
        spec_layout = QFormLayout()

        self.excitation_wavelength = QDoubleSpinBox()
        self.excitation_wavelength.setMinimum(0)
        self.excitation_wavelength.setMaximum(2000)
        self.excitation_wavelength.setDecimals(1)
        self.excitation_wavelength.setValue(532.0)
        self.excitation_wavelength.setSuffix(" nm")

        spec_layout.addRow("Excitation Wavelength:", self.excitation_wavelength)

        
        self.center_wavelength = QDoubleSpinBox()
        self.center_wavelength.setMinimum(0)
        self.center_wavelength.setMaximum(2000)
        self.center_wavelength.setDecimals(1)
        self.center_wavelength.setValue(545.0) 
        self.center_wavelength.setSuffix(" nm")

        self.exposure = QDoubleSpinBox()
        self.exposure.setMinimum(0.01)
        self.exposure.setMaximum(60)
        self.exposure.setDecimals(2)
        self.exposure.setSuffix(" s")
        self.exposure.setValue(1.0)

        self.slit_position = QDoubleSpinBox()
        self.slit_position.setMinimum(0)
        self.slit_position.setMaximum(10)
        self.slit_position.setDecimals(2)
        self.slit_position.setSuffix(" mm")
        self.slit_position.setValue(0.1)

        self.grating_combo = QComboBox()
        self.grating_combo.addItems(GRATING_CHOICES.keys())
        self.grating_combo.setCurrentText('Third (150 grooves/mm)') 
        
        spec_layout.addRow("Center Wavelength:", self.center_wavelength)
        spec_layout.addRow("Exposure:", self.exposure)
        spec_layout.addRow("Slit Position:", self.slit_position)
        spec_layout.addRow("Grating:", self.grating_combo)
        spec_box.setLayout(spec_layout)
        controls_layout.addWidget(spec_box)
        
        ccd_box = QGroupBox("CCD Parameters")
        ccd_layout = QFormLayout()
        
        self.gain_combo = QComboBox()
        self.gain_combo.addItems(GAIN_CHOICES.keys())
        self.gain_combo.setCurrentText('Best Dynamic Range') 
        
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(SPEED_CHOICES.keys())
        self.speed_combo.setCurrentText('50 kHz')  
        
        self.ccd_y_origin = QSpinBox()
        self.ccd_y_origin.setMinimum(0)
        self.ccd_y_origin.setMaximum(256)
        self.ccd_y_origin.setValue(0)

        self.ccd_y_size = QSpinBox()
        self.ccd_y_size.setMinimum(1)
        self.ccd_y_size.setMaximum(256)
        self.ccd_y_size.setValue(256)

        self.ccd_x_bin = QSpinBox()
        self.ccd_x_bin.setMinimum(1)
        self.ccd_x_bin.setMaximum(1024)
        self.ccd_x_bin.setValue(1)
        
        ccd_layout.addRow("Gain:", self.gain_combo)
        ccd_layout.addRow("Speed:", self.speed_combo)
        ccd_layout.addRow("CCD Y Origin (px):", self.ccd_y_origin) 
        ccd_layout.addRow("CCD Y Size (px):", self.ccd_y_size)
        ccd_layout.addRow("CCD X Bin (px):", self.ccd_x_bin)
        
        ccd_box.setLayout(ccd_layout)
        controls_layout.addWidget(ccd_box)

        rot_box = QGroupBox("Rotation Stage")
        rot_layout = QFormLayout()
        
        self.rotation_angle = QDoubleSpinBox()
        self.rotation_angle.setMinimum(-360)
        self.rotation_angle.setMaximum(360)
        self.rotation_angle.setDecimals(2)
        self.rotation_angle.setSuffix(" deg")
        self.rotation_angle.setValue(self.controller.last_angle)
        
        self.set_angle_button = QPushButton("Go to Angle")
        self.set_angle_button.clicked.connect(self.go_to_angle)
        rot_layout.addRow("Target Angle:", self.rotation_angle)
        rot_layout.addRow(self.set_angle_button)
        rot_box.setLayout(rot_layout)
        controls_layout.addWidget(rot_box)
        
        controls_layout.addStretch() 
        
        control_widget = QWidget()
        control_widget.setLayout(controls_layout)
        
        main_layout.addWidget(control_widget, 1)
        main_layout.addWidget(plot_widget, 3)    
        self.setLayout(main_layout)
        
        self.data_ready.connect(self.update_plot)
        self.image_ready.connect(self.update_image)
        self.scan_error.connect(self.handle_scan_error)
        self.connection_changed.connect(self._on_connection_changed)
        self.temp_updated.connect(self._on_temp_update)
        self.angle_updated.connect(self._on_angle_updated)

        # Apply initial hardware state to UI.
        if self._hw_ready:
            self.start_button.setEnabled(True)
            self.status_label.setText("Status: connected")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
        else:
            self.status_label.setText("Status: not connected")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")

        self._temp_timer = QtCore.QTimer(self)
        self._temp_timer.timeout.connect(self._trigger_temp_update)
        if self._hw_ready:
            self._temp_timer.start(5000)

        # Restore persisted axis choice + autoscale state, then wire
        # change signals to save automatically. Done last so all
        # widgets exist.
        self._restore_axis_settings()
        logger.info("RTC GUI initialized.")

    # ── Mode handling ─────────────────────────────────────────────────

    def _on_mode_changed(self, text: str) -> None:
        idx = 1 if text == "Image" else 0
        self.plot_stack.setCurrentIndex(idx)

    @QtCore.pyqtSlot(object)
    def update_image(self, arr) -> None:
        try:
            self.image_view.setImage(arr, autoLevels=True)
        except Exception as e:
            logger.warning(f"Failed to update image: {e}")

    # ── X-axis conversion ─────────────────────────────────────────────

    HC_NM_EV = 1239.841984  # vacuum hc in nm·eV

    def _convert_x(self, wl_nm):
        """Map wavelength array (nm) → (x_array, axis_label) according
        to the current x-axis combo selection.

        np.array(...) always copies — we never mutate the caller's array.
        """
        arr = np.array(wl_nm, dtype=float)
        mode = self.x_axis_combo.currentText()
        exc = self.excitation_wavelength.value()
        if mode == "Wavelength (nm)":
            return arr, mode
        if mode == "Raman shift (cm⁻¹)":
            try:
                return (1.0 / exc - 1.0 / arr) * 1e7, mode
            except (ZeroDivisionError, ValueError):
                return arr, mode
        if mode == "Energy (eV)":
            try:
                return self.HC_NM_EV / arr, mode
            except (ZeroDivisionError, ValueError):
                return arr, mode
        if mode == "Raman shift (eV)":
            try:
                return (self.HC_NM_EV / exc) - (self.HC_NM_EV / arr), mode
            except (ZeroDivisionError, ValueError):
                return arr, mode
        return arr, mode

    def _on_x_axis_changed(self, _text: str) -> None:
        # Update the axis label and re-render the latest data.
        if self.latest_wavelength is not None:
            self.update_plot(self.latest_wavelength, self.latest_intensity)
        self._save_axis_settings()

    # ── Auto-scale toggle ─────────────────────────────────────────────

    def _on_autoscale_toggled(self, checked: bool) -> None:
        vb = self.plot_item.getViewBox()
        if checked:
            vb.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)
            # Force an immediate refit so the next pushed frame
            # supersedes any stale view range from the disabled period.
            vb.autoRange()
        else:
            vb.disableAutoRange(axis=pg.ViewBox.XYAxes)
        self._save_axis_settings()

    # ── Persistence ───────────────────────────────────────────────────

    def _settings(self):
        from PyQt5.QtCore import QSettings
        return QSettings(
            QSettings.IniFormat, QSettings.UserScope,
            "HoribaIHR550", "RTC",
        )

    def _restore_axis_settings(self) -> None:
        s = self._settings()
        axis = s.value("x_axis")
        if axis is not None:
            idx = self.x_axis_combo.findText(str(axis))
            if idx >= 0:
                self.x_axis_combo.blockSignals(True)
                self.x_axis_combo.setCurrentIndex(idx)
                self.x_axis_combo.blockSignals(False)
                # Make sure the label reflects the restored selection.
                self.plot_item.setLabels(bottom=self.x_axis_combo.currentText())
        autoscale = s.value("autoscale")
        if autoscale is not None:
            on = (str(autoscale).lower() in ("true", "1"))
            self.autoscale_button.blockSignals(True)
            self.autoscale_button.setChecked(on)
            self.autoscale_button.blockSignals(False)
            self._on_autoscale_toggled(on)

    def _save_axis_settings(self) -> None:
        s = self._settings()
        s.setValue("x_axis", self.x_axis_combo.currentText())
        s.setValue("autoscale", self.autoscale_button.isChecked())
        s.sync()

    def enumconv(self, param_name: str, value: str):
        if param_name == 'grating':
            return GRATING_CHOICES[value].value
        enum_dict = PARAM_MAP.get(param_name)
        if enum_dict and value in enum_dict:
            return enum_dict[value].value
        logger.error(f"Unknown parameter or value: {param_name}={value}")
        return None

    def _start_event_loop(self):
        def run_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
        
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=run_loop, args=(self.loop,), daemon=True)
        self.loop_thread.start()
        logger.info("Event loop started in background thread")

    def run_async_task(self, task, timeout=30):
        try:
            future = asyncio.run_coroutine_threadsafe(task, self.loop)
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Error running async task: {e}")
            raise

    def go_to_angle(self):
        if not self._hw_ready:
            logger.warning("RTC: cannot move rotation stage — hardware not connected")
            return
        target = self.rotation_angle.value()

        async def _set_and_update():
            await self.controller.set_rotation_angle(target)
            await asyncio.sleep(0.5)
            return await self.controller.get_rotation_angle()

        future = asyncio.run_coroutine_threadsafe(_set_and_update(), self.loop)
        future.add_done_callback(self._handle_angle_result)

    def _handle_angle_result(self, fut):
        try:
            angle = fut.result()
            logger.info(f"RTC: fetched angle from hardware: {angle:.2f}°")
            self.angle_updated.emit(angle)
        except Exception as e:
            logger.error(f"RTC: rotation angle error: {e}")

    @QtCore.pyqtSlot(float)
    def _on_angle_updated(self, angle):
        self.rotation_angle.setValue(angle)

    def get_current_params(self):
        params = {
            'excitation_wavelength': self.excitation_wavelength.value(),
            'center_wavelength': self.center_wavelength.value(),
            'exposure': self.exposure.value(),
            'grating': self.enumconv('grating', self.grating_combo.currentText()),
            'slit_position': self.slit_position.value(),
            'gain': self.enumconv('gain', self.gain_combo.currentText()),
            'speed': self.enumconv('speed', self.speed_combo.currentText()),
            'rotation_angle': self.rotation_angle.value(),
            'ccd_y_origin': self.ccd_y_origin.value(),
            'ccd_y_size': self.ccd_y_size.value(),
            'ccd_x_bin': self.ccd_x_bin.value(),
        }
        return params

    def start_scan(self):
        if not self._hw_ready:
            logger.warning("Hardware not ready — cannot start scan.")
            return
        if self.is_scanning:
            logger.warning("Scan already running. Please stop current scan first.")
            return
            
        try:
            params = self.get_current_params()
        except Exception as e:
            logger.error(f"Invalid parameters: {e}")
            return
            
        self.stop_event.clear()
        self.is_scanning = True
        self.scanning_changed.emit(True)
        self.worker_thread = threading.Thread(
            target=self._scan_loop,
            args=(params,),
            daemon=True
        )

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.worker_thread.start()
        logger.info("Live scan started.")

    def stop_scan(self):
        logger.info("Stop requested by user.")
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)

        was_scanning = self.is_scanning
        self.is_scanning = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if was_scanning:
            self.scanning_changed.emit(False)
        logger.info("Live scan stopped.")

    def _scan_loop(self, params):
        try:
            logger.info(f"Setting angle to {params['rotation_angle']}° for scan")
            self.run_async_task(
                self.controller.set_rotation_angle(params['rotation_angle'])
            )
        except Exception as e:
            logger.error(f"Failed to set rotation angle: {e}")
            self.scan_error.emit(f"Failed to set rotation angle: {e}")
            return

        # The mode combo lives on the GUI thread; capturing its value
        # once per acquisition lets the user flip mode mid-scan and the
        # next iteration picks it up.
        acquisition_count = 0
        while not self.stop_event.is_set():
            try:
                acquisition_count += 1
                mode = self.mode_combo.currentText()
                logger.info(f"Starting acquisition #{acquisition_count} ({mode})")
                start_time = time.time()

                if mode == "Image":
                    arr = self.run_async_task(
                        self.controller.acquire_image(**params),
                        timeout=120,
                    )
                    if not self.stop_event.is_set():
                        self.image_ready.emit(arr)
                else:
                    x, y = self.run_async_task(
                        self.controller.acquire_spectrum(**params),
                        timeout=60,
                    )
                    if isinstance(x, list) and len(x) == 1:
                        x = x[0]
                    if isinstance(y, list) and len(y) == 1:
                        y = y[0]
                    if not self.stop_event.is_set():
                        self.data_ready.emit(x, y)

                logger.success(f"Acquisition #{acquisition_count} completed successfully")

                elapsed = time.time() - start_time
                logger.debug(f"Acquisition took {elapsed:.2f}s")

                if elapsed < 0.1:
                    time.sleep(0.1)

            except Exception as e:
                logger.error(f"Error in acquisition loop: {e}")
                self.scan_error.emit(f"Acquisition error: {e}")
                self.stop_event.set()
                break
        
        logger.info(f"Scan loop finishing after {acquisition_count} acquisitions.")
        QtCore.QTimer.singleShot(0, self.stop_scan)

    @QtCore.pyqtSlot(str)
    def handle_scan_error(self, error_msg):
        """Handle errors that occur in the scan loop"""
        logger.error(f"Scan error handler called: {error_msg}")
        self.stop_scan()

    def _connect_in_background(self):
        """Fire connect_hardware without blocking the UI. Emits connection_changed."""
        async def _do_connect():
            try:
                await self.controller.connect_hardware()
                self.connection_changed.emit(True, "connected")
            except Exception as e:
                logger.error(f"Hardware connection failed: {e}")
                self.connection_changed.emit(False, str(e))
        asyncio.run_coroutine_threadsafe(_do_connect(), self.loop)

    @QtCore.pyqtSlot(bool, str)
    def _on_connection_changed(self, ok: bool, message: str):
        self._hw_ready = ok
        if ok:
            self.status_label.setText("Status: connected")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.start_button.setEnabled(True)
            self._temp_timer.start(5000)
        else:
            self.status_label.setText(f"Status: not connected — {message}")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.start_button.setEnabled(False)
            self._temp_timer.stop()

    def _trigger_temp_update(self):
        if not self._hw_ready or not self.controller.is_connected:
            return
        # Don't poll the CCD mid-acquisition — controller already guards this,
        # but skipping here avoids a queued no-op.
        if self.is_scanning:
            return
        if self._temp_pending:
            return
        self._temp_pending = True
        future = asyncio.run_coroutine_threadsafe(
            self.controller.get_ccd_temperature(), self.loop
        )
        future.add_done_callback(self._temp_result_cb)

    def _temp_result_cb(self, fut):
        self._temp_pending = False
        try:
            self.temp_updated.emit(fut.result())
        except Exception:
            self.temp_updated.emit(-999.0)

    @QtCore.pyqtSlot(float)
    def _on_temp_update(self, temp: float):
        if temp == -999.0:
            self.temp_label.setText("CCD Temp: Err")
        else:
            color = "green" if temp < -50 else "red"
            self.temp_label.setText(
                f"CCD Temp: <font color='{color}'>{temp:.1f} °C</font>"
            )

    @QtCore.pyqtSlot(object, object)
    def update_plot(self, x_data, y_data):
        try:
            if len(x_data) > 0 and len(y_data) > 0:
                self.latest_wavelength = np.array(x_data)
                self.latest_intensity = np.array(y_data)
                x_plot, label = self._convert_x(self.latest_wavelength)
                self.plot_item.setLabels(bottom=label)
                self.plot_data_item.setData(x_plot, self.latest_intensity)
        except Exception as e:
            logger.warning(f"Failed to update plot: {e}")

    def closeEvent(self, event):
        logger.info("Closing RTC window...")
        self.stop_scan()

        # Always abort any in-flight CCD acquisition so we never leave
        # the device busy when control returns to the parent window.
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.controller.acquisition_abort(), self.loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.warning(f"acquisition_abort during close failed: {e}")

        # Only tear down the controller and the event loop when this
        # window owns them. When the main GUI injected its own
        # controller, the controller MUST keep running so the next
        # scan can fire without a 10 s ICL reboot.
        if self._owns_controller:
            try:
                logger.info("Shutting down Horiba controller...")
                future = asyncio.run_coroutine_threadsafe(
                    self.controller.shutdown(), self.loop
                )
                future.result(timeout=5)
                logger.info("Controller shutdown complete.")
            except Exception as e:
                logger.error(f"Error during controller shutdown: {e}")
            finally:
                if self.loop and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.loop.stop)
                    if self.loop_thread:
                        self.loop_thread.join(timeout=2)

        event.accept()

if __name__ == "__main__":
    from logging_setup import setup_file_logging
    log_path = setup_file_logging("rtc")
    logger.info(f"Logging to {log_path}")
    print(f"[rtc] log file: {log_path}", flush=True)

    app = QApplication(sys.argv)
    window = LiveViewWindow()
    window.show()
    sys.exit(app.exec_())