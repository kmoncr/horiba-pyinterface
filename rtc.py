import sys
import os
import asyncio
import threading
import time
from loguru import logger

from PyQt5 import QtWidgets, QtCore
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QPushButton, QDoubleSpinBox, QComboBox, QSpinBox
)
import pyqtgraph as pg

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
    scan_error = QtCore.pyqtSignal(str)  # Error message signal

    def __init__(self):
        super().__init__()
        
        self.controller = HoribaController(enable_logging=True)
        self.loop = None
        self.loop_thread = None
        self._start_event_loop()
        
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.is_scanning = False  # Track scanning state
        
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
        
        self.stop_button = QPushButton("STOP")
        self.stop_button.setStyleSheet("background-color: #f44336; color: white; font-weight: bold;")
        self.stop_button.clicked.connect(self.stop_scan)
        self.stop_button.setEnabled(False)
        
        scan_layout.addWidget(self.start_button)
        scan_layout.addWidget(self.stop_button)
        scan_box.setLayout(scan_layout)
        controls_layout.addWidget(scan_box)

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
        self.exposure.setValue(1.0)
        self.exposure.setMinimum(0.01)
        self.exposure.setMaximum(60)
        self.exposure.setDecimals(2)
        self.exposure.setSuffix(" s")

        self.slit_position = QDoubleSpinBox()
        self.slit_position.setValue(0.1)
        self.slit_position.setMinimum(0)
        self.slit_position.setMaximum(10)
        self.slit_position.setDecimals(2)
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
        self.ccd_y_origin.setValue(0)  
        self.ccd_y_origin.setMinimum(0)
        self.ccd_y_origin.setMaximum(256)
        
        self.ccd_y_size = QSpinBox()
        self.ccd_y_size.setValue(256)  
        self.ccd_y_size.setMinimum(1)
        self.ccd_y_size.setMaximum(256) 
        self.ccd_y_size.setValue(256)

        self.ccd_y_bin = QSpinBox()
        self.ccd_y_bin.setValue(256) 
        self.ccd_y_bin.setMinimum(1)
        self.ccd_y_bin.setMaximum(256)

        self.ccd_x_bin = QSpinBox()
        self.ccd_x_bin.setValue(1) 
        self.ccd_x_bin.setMinimum(1)
        self.ccd_x_bin.setMaximum(1024)
        
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
        self.rotation_angle.setValue(self.controller.last_angle)
        self.rotation_angle.setMinimum(-360)
        self.rotation_angle.setMaximum(360)
        self.rotation_angle.setDecimals(2)
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
        
        self.data_ready.connect(self.update_plot)
        self.scan_error.connect(self.handle_scan_error)
        logger.info("RTC GUI initialized.")

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
        if self.is_scanning:
            logger.warning("Cannot move stage while scan is active. Stop scan first.")
            return
        
        angle = self.rotation_angle.value()
        logger.info(f"Setting rotation angle to {angle}°")
        try:
            self.run_async_task(self.controller.set_rotation_angle(angle))
            logger.info("Angle set.")
        except Exception as e:
            logger.error(f"Failed to set angle: {e}")

    def start_scan(self):
        # FIXED: Prevent multiple simultaneous scans
        if self.is_scanning:
            logger.warning("Scan already running. Please stop current scan first.")
            return
            
        try:
            params = self.get_current_params()
        except Exception as e:
            logger.error(f"Invalid parameters: {e}")
            return
            
        self.stop_event.clear()
        self.is_scanning = True  # Set scanning flag
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
            self.worker_thread.join(timeout=5.0)  # Increased timeout
        
        self.is_scanning = False  # Clear scanning flag
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
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

        acquisition_count = 0
        while not self.stop_event.is_set():
            try:
                acquisition_count += 1
                logger.info(f"Starting acquisition #{acquisition_count}")
                start_time = time.time()
                
                x, y = self.run_async_task(
                    self.controller.acquire_spectrum(**params),
                    timeout=60  # Increased timeout for acquisition
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
                
                # Small delay between acquisitions
                if elapsed < 0.1:
                    time.sleep(0.1)
                        
            except Exception as e:
                logger.error(f"Error in acquisition loop: {e}")
                self.scan_error.emit(f"Acquisition error: {e}")
                self.stop_event.set()
                break
        
        logger.info(f"Scan loop finishing after {acquisition_count} acquisitions.")
        # Ensure UI is updated
        QtCore.QTimer.singleShot(0, self.stop_scan)

    @QtCore.pyqtSlot(str)
    def handle_scan_error(self, error_msg):
        """Handle errors that occur in the scan loop"""
        logger.error(f"Scan error handler called: {error_msg}")
        self.stop_scan()

    @QtCore.pyqtSlot(object, object)
    def update_plot(self, x_data, y_data):
        try:
            if len(x_data) > 0 and len(y_data) > 0:
                self.plot_data_item.setData(x_data, y_data)
        except Exception as e:
            logger.warning(f"Failed to update plot: {e}")

    def closeEvent(self, event):
        logger.info("Closing application...")
        self.stop_scan()
        
        try:
            logger.info("Shutting down Horiba controller...")
            future = asyncio.run_coroutine_threadsafe(
                self.controller.shutdown(), 
                self.loop
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
    app = QApplication(sys.argv)
    window = LiveViewWindow()
    window.show()
    sys.exit(app.exec_())