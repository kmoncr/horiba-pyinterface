import os
import sys
import asyncio
from loguru import logger
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import (
    QLabel, QHBoxLayout, QGroupBox, QComboBox, 
    QPushButton, QVBoxLayout, QDoubleSpinBox, QFormLayout
)
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
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed', 'grating', 'rotation_angle'
            ],
            displays=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed', 'grating', 'rotation_angle'
            ],
            x_axis='Wavelength',
            y_axis='Intensity',
            sequencer=True,
            sequencer_inputs=['rotation_angle'],
        )
        self.setWindowTitle('Horiba Spectrum Scan')

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.controller = HoribaController(enable_logging=True)
        
        # Set default rotation angle in GUI to last known angle
        self.inputs.rotation_angle.setValue(self.controller.last_angle) 

        # Grating Control Box
        grating_widget = QGroupBox("Grating Control")
        grating_layout = QHBoxLayout()
        self.grating_combo = QComboBox()
        self.grating_combo.addItems(GRATING_CHOICES.keys())
        self.grating_combo.setCurrentText('Third (150 grooves/mm)')
        self.grating_combo.currentTextChanged.connect(self.update_grating)
        grating_layout.addWidget(QLabel("Current Grating:"))
        grating_layout.addWidget(self.grating_combo)
        grating_widget.setLayout(grating_layout)

        # --- Custom Input for Scans Per Angle ---
        scan_count_widget = QGroupBox("Scan Sequence Control")
        scan_count_layout = QFormLayout()
        self.scans_per_angle_input = QtWidgets.QSpinBox()
        self.scans_per_angle_input.setRange(1, 100)
        self.scans_per_angle_input.setValue(1) 
        scan_count_layout.addRow("Scans per Angle:", self.scans_per_angle_input)
        scan_count_widget.setLayout(scan_count_layout)
        # --- End Custom Input ---
        
        # --- Rotation Stage Control Box ---
        rotation_widget = QGroupBox("Rotation Stage Control")
        rotation_layout = QVBoxLayout()
        
        # Current Angle Display
        current_angle_layout = QHBoxLayout()
        self.current_angle_display = QLabel("Current Angle: --.-°")
        self.refresh_angle_button = QPushButton("Refresh")
        self.refresh_angle_button.clicked.connect(self.update_current_angle)
        current_angle_layout.addWidget(self.current_angle_display)
        current_angle_layout.addWidget(self.refresh_angle_button)
        rotation_layout.addLayout(current_angle_layout)

        # Manual Angle Input
        set_angle_layout = QFormLayout()
        self.set_angle_input = QDoubleSpinBox()
        self.set_angle_input.setRange(-360.0, 360.0)
        self.set_angle_input.setDecimals(2)
        self.set_angle_input.setValue(self.controller.last_angle)
        self.go_to_angle_button = QPushButton("Go to Angle")
        self.go_to_angle_button.clicked.connect(self.do_go_to_angle)
        set_angle_layout.addRow("Set Angle (deg):", self.set_angle_input)
        set_angle_layout.addRow(self.go_to_angle_button)
        rotation_layout.addLayout(set_angle_layout)
        
        # Return to Origin
        self.return_to_origin_button = QPushButton("Return to Origin (0°)")
        self.return_to_origin_button.clicked.connect(self.do_return_to_origin)
        rotation_layout.addWidget(self.return_to_origin_button)

        rotation_widget.setLayout(rotation_layout)
        # --- End of Rotation Box ---

        # Add widgets to main layout
        self.inputs.layout().addWidget(grating_widget)
        self.inputs.layout().addWidget(scan_count_widget) # Added the scan count control
        self.inputs.layout().addWidget(rotation_widget)

        self.file_input.extensions = ['csv']
        
        # Get initial angle
        self.update_current_angle()

    def run_async_task(self, task):
        """Helper to run async tasks from sync GUI methods"""
        try:
            # Check if loop is running
            if self.loop.is_running():
                # Schedule task if loop is already running (e.g., during experiment)
                asyncio.run_coroutine_threadsafe(task, self.loop)
            else:
                # Run task to completion if loop is not running
                self.loop.run_until_complete(task)
        except Exception as e:
            logger.error(f"Error running async task: {e}")

    def update_current_angle(self):
        """Get angle from controller and update GUI label"""
        logger.debug("Requesting current angle update...")
        async def _get_angle():
            angle = await self.controller.get_rotation_angle()
            self.current_angle_display.setText(f"Current Angle: {angle:.2f}°")
            self.set_angle_input.setValue(angle) # Update manual input box
            self.inputs.rotation_angle.setValue(angle) # Update procedure input
            logger.info(f"GUI updated with angle: {angle:.2f}°")
        
        self.run_async_task(_get_angle())

    def do_go_to_angle(self):
        """Set controller angle from manual input box"""
        target_angle = self.set_angle_input.value()
        logger.info(f"GUI: Setting angle to {target_angle}°")
        self.run_async_task(self.controller.set_rotation_angle(target_angle))
        # We optimistically update the display. update_current_angle() can confirm.
        self.current_angle_display.setText(f"Current Angle: {target_angle:.2f}°")
        self.inputs.rotation_angle.setValue(target_angle) # Update procedure input

    def do_return_to_origin(self):
        """Tell controller to return to origin"""
        logger.info("GUI: Returning to origin")
        self.run_async_task(self.controller.return_rotation_to_origin())
        self.current_angle_display.setText("Current Angle: 0.00°")
        self.set_angle_input.setValue(0.0)
        self.inputs.rotation_angle.setValue(0.0) # Update procedure input


    def update_grating(self, text):
        logger.info(f"GUI: Grating changed to {text}")

    def make_procedure(self, rotation_angle=None):
        """Creates a single procedure instance for one scan."""
        procedure = self.procedure_class()
        procedure.controller = self.controller
        procedure.loop = self.loop

        param_names = [
            "excitation_wavelength", "center_wavelength", "exposure",
            "slit_position", "gain", "speed", "rotation_angle"
        ]
        
        for param_name in param_names:
            if hasattr(self.inputs, param_name):
                value = getattr(self.inputs, param_name).value()
                setattr(procedure, param_name, value)
                logger.info(f"  {param_name}: {value}")
        
        # Override rotation angle if provided (used by queue/sequencer)
        if rotation_angle is not None:
            setattr(procedure, 'rotation_angle', rotation_angle)
            logger.info(f"  rotation_angle (sequenced): {rotation_angle}")

        procedure.grating = self.grating_combo.currentText()
        logger.info(f"  grating: {procedure.grating}")
        
        return procedure

    def queue(self, procedure=None):
        """Queues one or more experiments based on scans_per_angle setting."""
        
        # The sequencer/manual queueing provides the base procedure with the correct angle
        if procedure is None:
            # If called manually (not by sequencer), create the base procedure
            procedure = self.make_procedure() 
        
        # Get the number of scans to perform at this angle
        scans_per_angle = self.scans_per_angle_input.value()
        base_rotation_angle = procedure.rotation_angle

        # Loop to queue multiple experiments
        for i in range(1, scans_per_angle + 1):
            
            # Create a new, unique procedure instance for each scan, forcing the sequenced angle
            current_procedure = self.make_procedure(rotation_angle=base_rotation_angle)
            
            # Generate a unique filename including both angle and scan number
            filename = self.unique_filename(
                self.file_input.directory, 
                self.file_input.filename, 
                current_procedure.rotation_angle, 
                i # Pass scan number for unique filename
            )
            current_procedure.data_filename = filename
            
            experiment = self.new_experiment(Results(current_procedure, filename))
            self.manager.queue(experiment)
        
        # Remember last angle in GUI inputs (only need to do this once per angle step)
        self.inputs.rotation_angle.setValue(base_rotation_angle)
        
        # Update current angle display
        self.update_current_angle()
        
        sleep(2) # Original sleep

    def unique_filename(self, directory, base_filename, rotation_angle, scan_number):
        """Generates a unique filename using 0.0deg format and scan number."""
        counter = 1
        # Format angle using one decimal place and "deg" suffix
        angle_str = f"{rotation_angle:.1f}deg"
        
        # Filename format: [base_filename]_[angle.Xdeg]_S[scan_number]_[counter].csv
        filename = f"{base_filename}_{angle_str}_S{scan_number}_{counter}.csv"
        file_path = os.path.join(directory, filename)

        # Ensure filename is globally unique (in case same angle/scan number is used multiple times)
        while os.path.exists(file_path):
            counter += 1
            filename = f"{base_filename}_{angle_str}_S{scan_number}_{counter}.csv"
            file_path = os.path.join(directory, filename)
        
        logger.info(f"Generated unique filename: {file_path}")
        return file_path

    def closeEvent(self, event):
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
