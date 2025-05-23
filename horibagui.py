import datetime
import sys
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import QLabel, QSpinBox, QHBoxLayout, QWidget, QSizePolicy
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
            y_axis="Intensity"
        )
        self.setWindowTitle("horiba spectrum scan")

        scan_count_label = QLabel("Number of scans:")
        scan_count_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.scan_count_spinbox = QSpinBox()
        self.scan_count_spinbox.setMinimum(1)
        self.scan_count_spinbox.setValue(1)
        self.scan_count_spinbox.setFixedWidth(60)
        self.scan_count_spinbox.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

# Put them side by side in a QWidget with QHBoxLayout
        scan_widget = QWidget()
        scan_layout = QHBoxLayout()
        scan_layout.setContentsMargins(0, 0, 0, 0)
        scan_layout.setSpacing(5)
        scan_layout.addWidget(scan_count_label)
        scan_layout.addWidget(self.scan_count_spinbox)
        scan_layout.addStretch()  # push left
        scan_widget.setLayout(scan_layout)

# Add this widget to the vertical inputs layout
        self.inputs.layout().addWidget(scan_widget)



    def make_filename(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"spectrum_data_{ts}.csv"
        return filename
    
    def queue(self):
        num_scans = self.scan_count_spinbox.value()

        for i in range(num_scans):
            procedure = self.make_procedure()
            filename = self.make_filename()
            results = Results(procedure, filename)
            experiment = self.new_experiment(results)
            self.manager.queue(experiment)
            sleep(2)

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())