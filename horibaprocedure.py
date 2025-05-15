import numpy as np
import asyncio
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter
from horibacontroller import HoribaController

class HoribaSpectrumProcedure(Procedure):
    center_wavelength = FloatParameter("Center Wavelength (nm)", default=780)
    exposure         = IntegerParameter("Exposure (ms)",    default=1000)
    grating          = IntegerParameter("Grating",          default=3)
    slit             = IntegerParameter("Slit", default = 1)
    slit_position    = FloatParameter("Slit position(mm)",          default=0.1)
    mirror = IntegerParameter("Mirror", default = 1)
    mirror_position  = IntegerParameter("Mirror position",       default=0)
    gain             = IntegerParameter("Gain",             default=0)
    speed            = IntegerParameter("Speed",            default=2)

    DATA_COLUMNS = ["Wavenumber", "Intensity"]

    def execute(self):
        self.controller = HoribaController()
        x_data, y_data = asyncio.run(self.controller.initialize())
        
        for i in range(len(y_data)):
            for x, y in zip(x_data, y_data[i]):
                self.emit("results", {
                    "Wavenumber": 1/x,
                    "Intensity": y
                })