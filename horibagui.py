import datetime
import sys
from pymeasure.display.Qt import QtWidgets
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure
from horibacontroller import HoribaController

class MainWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                "start_wavelength", "end_wavelength", "step_size",
                "exposure", "grating", "slit_position",
                "mirror_position", "gain", "speed"
            ],
            displays=[
                "start_wavelength", "end_wavelength", "step_size",
                "exposure", "grating", "slit_position",
                "mirror_position", "gain", "speed"
            ],
            x_axis="Wavenumber (cm^-1)",
            y_axis="Intensity"
        )
        self.setWindowTitle("horiba spectrum scan")

    def make_filename(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"spectrum_data_{ts}.csv"

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())