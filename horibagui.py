import datetime
import sys
from pymeasure.display.Qt import QtWidgets
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure
from pymeasure.experiment import Results

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

    def make_filename(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"spectrum_data_{ts}.csv"
        return filename
    
    def queue(self):
        procedure = self.make_procedure()
        filename = self.make_filename()
        results = Results(procedure, filename)
        experiment = self.new_experiment(results)
        self.manager.queue(experiment)

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())