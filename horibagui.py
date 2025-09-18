import os
import sys
import asyncio
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import QLabel, QSpinBox, QHBoxLayout, QWidget, QSizePolicy, QVBoxLayout, QPushButton, QGroupBox
from PyQt5.QtCore import QTimer
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure
from horibacontroller import HoribaController
from pymeasure.experiment import Results
from time import sleep

class MainWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                "excitation_wavelength", "center_wavelength", "exposure", "grating", "slit_position",
                "gain", "speed", "rotation_angle"
            ],
            displays=[
                "excitation_wavelength",
                "center_wavelength",
                "exposure", "grating", "slit_position",
                "gain", "speed", "rotation_angle"
            ],
            x_axis="Wavelength",
            y_axis="Intensity", 
        )
        self.setWindowTitle("horiba spectrum scan")
        self.controller = HoribaController()
        self._shutdown_complete = False

         # Creating the combo box for the 'grating' parameter
        self.grating_combo = QComboBox()
        self.grating_combo.addItems(list(GRATING_CHOICES.keys()))  # Add the keys from your GRATING_CHOICES

        # Set the default value for the grating combo box
        default_grating = 'Third (150 grooves/mm)'
        self.grating_combo.setCurrentText(default_grating)

        # Connect the combo box change event to a method that updates the grating parameter
        self.grating_combo.currentTextChanged.connect(self.update_grating)

        grating_widget = QWidget()
        grating_layout = QHBoxLayout()
        grating_layout.addWidget(QLabel("Grating:"))
        grating_layout.addWidget(self.grating_combo)
        grating_widget.setLayout(grating_layout)

        # Add the custom grating widget to your layout
        self.inputs.layout().addWidget(grating_widget)

    def update_grating(self, selected_grating):
        # Update the grating parameter based on the selected item
        self.grating = GRATING_CHOICES[selected_grating]

        scan_count_label = QLabel("Number of scans:")
        scan_count_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.scan_count_spinbox = QSpinBox()
        self.scan_count_spinbox.setMinimum(1)
        self.scan_count_spinbox.setValue(1)
        self.scan_count_spinbox.setFixedWidth(60)
        self.scan_count_spinbox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        scan_widget = QWidget()
        scan_layout = QHBoxLayout()
        scan_layout.setContentsMargins(0, 0, 0, 0)
        scan_layout.setSpacing(5)
        scan_layout.addWidget(scan_count_label)
        scan_layout.addWidget(self.scan_count_spinbox)
        scan_layout.addStretch()  # push left
        scan_widget.setLayout(scan_layout)
        self.inputs.layout().addWidget(scan_widget)

        rotation_group = QGroupBox("Rotation Stage Controls")
        rotation_layout = QVBoxLayout()
        
        angle_widget = QWidget()
        angle_layout = QHBoxLayout()
        angle_layout.setContentsMargins(0, 0, 0, 0)
        
        self.current_angle_label = QLabel("Current: 0.0°")
        angle_layout.addWidget(self.current_angle_label)
        
        refresh_angle_btn = QPushButton("Refresh")
        refresh_angle_btn.clicked.connect(self.refresh_rotation_angle)
        refresh_angle_btn.setFixedWidth(60)
        angle_layout.addWidget(refresh_angle_btn)
        
        angle_layout.addStretch()
        angle_widget.setLayout(angle_layout)
        rotation_layout.addWidget(angle_widget)
        
        button_widget = QWidget()
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        
        origin_btn = QPushButton("Return to Origin")
        origin_btn.clicked.connect(self.return_to_origin)
        button_layout.addWidget(origin_btn)
        
        button_layout.addStretch()
        button_widget.setLayout(button_layout)
        rotation_layout.addWidget(button_widget)
        
        rotation_group.setLayout(rotation_layout)
        self.inputs.layout().addWidget(rotation_group)

        self.file_input.extensions = ['csv']
        
        self.angle_update_timer = QTimer()
        self.angle_update_timer.timeout.connect(self.refresh_rotation_angle)
        self.angle_update_timer.start(5000)  # Update every 5 seconds

    def refresh_rotation_angle(self):
        try:
            if hasattr(self.controller, 'rotation_stage') and self.controller.rotation_stage:
                angle = self.controller.get_rotation_angle()
                self.current_angle_label.setText(f"Current: {angle:.1f}°")
        except Exception as e:
            self.current_angle_label.setText("Current: Error")
            
    def return_to_origin(self):
        try:
            self.controller.return_rotation_to_origin()
            self.refresh_rotation_angle()
        except Exception as e:
            print(f"Error returning to origin: {e}")

    def make_procedure(self):
        procedure = self.procedure_class()
        procedure.controller = self.controller 
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
        if not self._shutdown_complete:
            self._shutdown_timer = QTimer()
            self._shutdown_timer.timeout.connect(self._check_shutdown_complete)
            self._shutdown_timer.start(100)  
            
            asyncio.create_task(self._shutdown_devices())
            
            event.ignore()
        else:
            super().closeEvent(event)

    async def _shutdown_devices(self):
        try:
            await self.controller.shutdown()
        except Exception as e:
            print(f"Error during shutdown: {e}")
        finally:
            self._shutdown_complete = True

    def _check_shutdown_complete(self):
        if self._shutdown_complete:
            self._shutdown_timer.stop()
            self.close() 

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())