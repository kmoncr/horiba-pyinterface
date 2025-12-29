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
    QCheckBox, QMessageBox
)
import pyqtgraph as pg
import numpy as np

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
    scan_error = QtCore.pyqtSignal(str)
    connection_success = QtCore.pyqtSignal() 

    def __init__(self):
        super().__init__()
        
        self.controller = None
        
        self.loop = None
        self.loop_thread = None
        self._start_event_loop()

        self.setup_ui()
        
        logger.info("Starting hardware connection in background...")
        threading.Thread(target=self._startup_routine, daemon=True).start()

        self.worker_thread = None
        self.stop_event = threading.Event()
        self.is_scanning = False  
        
        self.latest_wavelength = None
        self.latest_intensity = None
        
        self.data_ready.connect(self.update_plot)
        self.scan_error.connect(self.handle_scan_error)
        self.connection_success.connect(self.on_connection_success) 
        
        logger.info("RTC GUI initialized.")

    def setup_ui(self):
        self.setWindowTitle("Horiba RTC")
        self.setGeometry(100, 100, 1200, 700)
        
        main_layout = QHBoxLayout()
        controls_layout = QVBoxLayout()
        controls_layout.setSpacing(15)
        
        plot_widget = QWidget()
        plot_layout = QVBoxLayout()
        self.plot_widget = pg.PlotWidget()
        self.plot_item = self.plot_widget.getPlotItem()
        self.plot_item.setLabels(left='Intensity (counts)', bottom='Wavelength (nm)')
        self.plot_data_item = self.plot_item.plot(pen='y') 
        plot_layout.addWidget(self.plot_widget)
        plot_widget.setLayout(plot_layout)
    
        scan_box = QGroupBox("Scan Control")
        scan_layout = QHBoxLayout()
        self.start_button = QPushButton("START")
        self.start_button.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        self.start_button.clicked.connect(self.start_scan)
        self.start_button.setEnabled(False) 
        
        self.stop_button = QPushButton("STOP")
        self.stop_button.setStyleSheet("background-color: #f44336; color: white; font-weight: bold;")
        self.stop_button.clicked.connect(self.stop_scan)
        self.stop_button.setEnabled(False)
        
        scan_layout.addWidget(self.start_button)
        scan_layout.addWidget(self.stop_button)
        scan_box.setLayout(scan_layout)
        controls_layout.addWidget(scan_box)

        plot_options_box = QGroupBox("Plot Options")
        plot_options_layout = QFormLayout()
        self.wavenumber_checkbox = QCheckBox()
        self.wavenumber_checkbox.setChecked(False)
        self.wavenumber_checkbox.stateChanged.connect(self.toggle_x_axis)
        plot_options_layout.addRow("Plot in Wavenumber (cm⁻¹):", self.wavenumber_checkbox)
        plot_options_box.setLayout(plot_options_layout)
        controls_layout.addWidget(plot_options_box)

        spec_box = QGroupBox("Spectrometer Parameters")
        spec_layout = QFormLayout()

        self.excitation_wavelength = QDoubleSpinBox()
        self.excitation_wavelength.setRange(0, 2000)
        self.excitation_wavelength.setValue(532.0)
        self.excitation_wavelength.setSuffix(" nm")
        spec_layout.addRow("Excitation Wavelength:", self.excitation_wavelength)

        self.center_wavelength = QDoubleSpinBox()
        self.center_wavelength.setRange(0, 2000)
        self.center_wavelength.setValue(545.0) 
        self.center_wavelength.setSuffix(" nm")

        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(0.01, 60)
        self.exposure.setValue(1.0)
        self.exposure.setSuffix(" s")

        self.slit_position = QDoubleSpinBox()
        self.slit_position.setRange(0, 10)
        self.slit_position.setValue(0.1)
        self.slit_position.setSuffix(" mm")

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
        self.ccd_y_origin.setRange(0, 256)
        self.ccd_y_origin.setValue(0)
        
        self.ccd_y_size = QSpinBox()
        self.ccd_y_size.setRange(1, 256)
        self.ccd_y_size.setValue(256)

        self.ccd_x_bin = QSpinBox()
        self.ccd_x_bin.setRange(1, 1024)
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
        self.rotation_angle.setValue(0.0)
        self.rotation_angle.setRange(-360, 360)
        self.rotation_angle.setSuffix(" deg")
        
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

    def _startup_routine(self):
        """Run initialization inside the async loop context"""
        async def async_init():
            try:
                logger.info("instantiating controller on background thread...")
                self.controller = HoribaController(enable_logging=True)
                
                # Connect
                logger.info("connecting to hardware...")
                await self.controller.connect_hardware()
                
                return True
            except Exception as e:
                logger.error(f"init failed: {e}")
                self.scan_error.emit(f"init failed; check if icl is running. Error: {e}")
                return False

        success = self.run_async_task(async_init(), timeout=60)
        
        if success:
            logger.success("Hardware connected successfully.")
            self.connection_success.emit()

    @QtCore.pyqtSlot()
    def on_connection_success(self):
        """Called on main thread after hardware connects"""
        self.start_button.setEnabled(True)
        if self.controller:
            try:
                self.rotation_angle.setValue(self.controller.last_angle)
            except:
                pass

    def wavelength_to_wavenumber(self, wavelength_nm):
        excitation = self.excitation_wavelength.value()
        try:
            wavenumber = (1.0 / excitation - 1.0 / wavelength_nm) * 1e7
            return wavenumber
        except (ZeroDivisionError, TypeError):
            return wavelength_nm 

    def toggle_x_axis(self):
        if self.wavenumber_checkbox.isChecked():
            self.plot_item.setLabels(bottom='Raman Shift (cm⁻¹)')
        else:
            self.plot_item.setLabels(bottom='Wavelength (nm)')
        
        if self.latest_wavelength is not None and self.latest_intensity is not None:
            self.update_plot(self.latest_wavelength, self.latest_intensity)

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

    def go_to_angle(self):
        if not self.controller:
            return
        if self.is_scanning:
            logger.warning("Cannot move stage while scan is active.")
            return
        
        angle = self.rotation_angle.value()
        logger.info(f"Setting rotation angle to {angle}°")
        try:
            self.run_async_task(self.controller.set_rotation_angle(angle))
            logger.info("Angle set.")
        except Exception as e:
            logger.error(f"Failed to set angle: {e}")

    def start_scan(self):
        if not self.controller:
            logger.error("Controller not initialized")
            return
        if self.is_scanning:
            return
            
        try:
            params = self.get_current_params()
        except Exception as e:
            logger.error(f"Invalid parameters: {e}")
            return
            
        self.stop_event.clear()
        self.is_scanning = True 
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
        
        self.is_scanning = False  
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        logger.info("Live scan stopped.")

    def _scan_loop(self, params):
        try:
            self.run_async_task(
                self.controller.set_rotation_angle(params['rotation_angle'])
            )
        except Exception as e:
            logger.error(f"Failed to set rotation angle: {e}")
            self.scan_error.emit(f"Failed to set rotation angle: {e}")
            return

        acquisition_count = 0
        while not self.stop_event.is_set():
            try:
                acquisition_count += 1
                # logger.info(f"Starting acquisition #{acquisition_count}")
                start_time = time.time()
                
                x, y = self.run_async_task(
                    self.controller.acquire_spectrum(**params),
                    timeout=60 
                )
                
                if isinstance(x, list) and len(x) == 1:
                    x = x[0]
                if isinstance(y, list) and len(y) == 1:
                    y = y[0]

                if not self.stop_event.is_set():
                    self.data_ready.emit(x, y)
                
                elapsed = time.time() - start_time
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
        logger.error(f"Scan error handler called: {error_msg}")
        QMessageBox.critical(self, "Hardware Error", error_msg)
        self.stop_scan()

    @QtCore.pyqtSlot(object, object)
    def update_plot(self, x_data, y_data):
        try:
            if len(x_data) > 0 and len(y_data) > 0:
                self.latest_wavelength = np.array(x_data)
                self.latest_intensity = np.array(y_data)
                
                if self.wavenumber_checkbox.isChecked():
                    x_plot = self.wavelength_to_wavenumber(self.latest_wavelength)
                else:
                    x_plot = self.latest_wavelength
                
                self.plot_data_item.setData(x_plot, self.latest_intensity)
        except Exception as e:
            logger.warning(f"Failed to update plot: {e}")

    def closeEvent(self, event):
        logger.info("Closing application...")
        self.stop_scan()
        
        if self.controller:
            try:
                logger.info("Shutting down Horiba controller...")
                future = asyncio.run_coroutine_threadsafe(
                    self.controller.shutdown(), 
                    self.loop
                )
                future.result(timeout=5)
            except Exception as e:
                logger.error(f"Error during controller shutdown: {e}")
        
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self.loop_thread:
                self.loop_thread.join(timeout=2)
            
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = LiveViewWindow()
    window.show()
    sys.exit(app.exec_())