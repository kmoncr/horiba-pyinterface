import datetime
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
    
    """def make_inputs(self):
        inputs() = super.make_inputs()
        controller = HoribaController()
        try: 
            gains_dict = controller.get_available_gains()
        finally: 
            self.abort()
        gain_labels = list(gains_dict.values())
        label_to_token = {v: k for k, v in gains_dict.items()}

        inputs["gain"] = ListInput("Gain", choices=gain_labels)"""

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    app.exec_()