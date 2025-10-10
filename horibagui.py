import os
import sys
import asyncio
from loguru import logger
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import (QLabel, QSpinBox, QHBoxLayout, QPushButton, QGroupBox, QComboBox)
from PyQt5.QtCore import QTimer
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure, GRATING_CHOICES
from horibacontroller import HoribaController
from pymeasure.experiment import Results
from time import sleep

class MainWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                "excitation_wavelength", "center_wavelength", "exposure",
                "slit_position", "gain", "speed"  
            ],
            displays=[
                "excitation_wavelength", "center_wavelength", "exposure",
                "slit_position", "gain", "speed"
            ],
            x_axis="Wavelength",
            y_axis="Intensity",
        )
        self.setWindowTitle("Horiba Spectrum Scan")

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.controller = HoribaController(enable_logging=True)
        
        try:
            self.loop.run_until_complete(self.controller._ensure_initialized())
        except Exception as e:
            logger.error(f"Failed to initialize controller: {e}")
            raise

        grating_widget = QGroupBox("Grating Control")
        grating_layout = QHBoxLayout()
        
        self.grating_combo = QComboBox()
        self.grating_combo.addItems(GRATING_CHOICES.keys())
        self.grating_combo.setCurrentText('Third (150 grooves/mm)')  
        self.grating_combo.currentTextChanged.connect(self.update_grating)
        
        grating_layout.addWidget(QLabel("Current Grating:"))
        grating_layout.addWidget(self.grating_combo)
        grating_widget.setLayout(grating_layout)

        rotation_widget = QGroupBox("Rotation Control")
        rotation_layout = QHBoxLayout()
        
        self.rotation_input = QSpinBox()
        self.rotation_input.setRange(0, 360)
        self.rotation_input.setValue(0)
        self.rotation_input.valueChanged.connect(self.update_rotation)
        
        self.return_origin_button = QPushButton("Return to Origin")
        self.return_origin_button.clicked.connect(self.return_to_origin)
        
        rotation_layout.addWidget(QLabel("Angle (deg):"))
        rotation_layout.addWidget(self.rotation_input)
        rotation_layout.addWidget(self.return_origin_button)
        rotation_widget.setLayout(rotation_layout)

        scan_widget = QGroupBox("Scan Control")
        scan_layout = QHBoxLayout()
        
        self.scan_count_spinbox = QSpinBox()
        self.scan_count_spinbox.setMinimum(1)
        self.scan_count_spinbox.setValue(1)
        self.scan_count_spinbox.setFixedWidth(60)
        
        scan_layout.addWidget(QLabel("Number of scans:"))
        scan_layout.addWidget(self.scan_count_spinbox)
        scan_layout.addStretch()
        scan_widget.setLayout(scan_layout)

        self.inputs.layout().addWidget(grating_widget)
        self.inputs.layout().addWidget(rotation_widget)
        self.inputs.layout().addWidget(scan_widget)

        self.rotation_timer = QTimer()
        self.rotation_timer.timeout.connect(self.refresh_rotation_angle)
        self.rotation_timer.start(1000)

        self.file_input.extensions = ['csv']

    def update_grating(self, text):
       logger.info(f"GUI: Grating changed to {text}")

    def update_rotation(self, value):
        if self.controller:
            '''logger.info(f"GUI: Rotation changed to {value}")'''
            try:
                self.loop.run_until_complete(self.controller.set_rotation_angle(float(value)))
            except Exception as e:
                logger.error(f"Failed to set rotation angle: {e}")

    def refresh_rotation_angle(self):
        if self.controller and hasattr(self.controller, "get_rotation_angle"):
            try:
                current_angle = self.loop.run_until_complete(self.controller.get_rotation_angle())
                if current_angle != self.rotation_input.value():
                    self.rotation_input.setValue(int(current_angle))
            except Exception as e:
                logger.error(f"Failed to get rotation angle: {e}")

    def return_to_origin(self):
        if self.controller and hasattr(self.controller, "return_rotation_to_origin"):
            try:
                self.loop.run_until_complete(self.controller.return_rotation_to_origin())
                self.rotation_input.setValue(0)
            except Exception as e:
                logger.error(f"Failed to return to origin: {e}")

    def make_procedure(self):
        procedure = self.procedure_class()
        procedure.controller = self.controller
        procedure.loop = self.loop

        input_list = [
            "excitation_wavelength", "center_wavelength", "exposure",
            "slit_position", "gain", "speed"
        ]
        
        logger.info("New procedure created with parameters:")
        for param_name in input_list:
            if hasattr(self.inputs, param_name):
                value = getattr(self.inputs, param_name).value()
                setattr(procedure, param_name, value)
                logger.info(f"  {param_name}: {value}")
        
        grating_text = self.grating_combo.currentText()
        procedure.grating = grating_text
        logger.info(f"  grating: {grating_text}")
        
        procedure.rotation_angle = float(self.rotation_input.value())
        logger.info(f"  rotation_angle: {procedure.rotation_angle}")
        
        return procedure

    def queue(self):
        num_scans = self.scan_count_spinbox.value()
        directory = self.file_input.directory
        base_filename = self.file_input.filename

        if not base_filename.lower().endswith('.csv'):
            base_filename += '.csv'

        filename_root, extension = os.path.splitext(base_filename)
        for i in range(num_scans):
            count = 1
            while True:
                unique_filename = f"{filename_root}_{count}{extension}"
                full_path = os.path.join(directory, unique_filename)
                if not os.path.exists(full_path):
                    break
                count += 1

            procedure = self.make_procedure()
            procedure.data_filename = full_path
            
            experiment = self.new_experiment(Results(procedure, procedure.data_filename))
            self.manager.queue(experiment)
            sleep(2)

    def closeEvent(self, event):
        self.rotation_timer.stop()
        if not self.loop.is_closed():
            try:
                self.loop.run_until_complete(self.controller.shutdown())
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
            finally:
                self.loop.close()
        event.accept()

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())