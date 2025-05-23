import os
import sys
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import QLabel, QSpinBox, QHBoxLayout, QWidget, QSizePolicy, QVBoxLayout
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure
from pymeasure.experiment import Results
from time import sleep

class MainWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                "excitation_wavelength", "center_wavelength", "exposure", "grating", "slit_position",
                "gain", "speed"
            ],
            displays=[
                "excitation_wavelength",
                "center_wavelength",
                "exposure", "grating", "slit_position",
                "gain", "speed"
            ],
            x_axis="Wavelength",
            y_axis="Intensity", 
        )
        self.setWindowTitle("horiba spectrum scan")

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

        self.file_input.extensions = ['csv']
    
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

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())