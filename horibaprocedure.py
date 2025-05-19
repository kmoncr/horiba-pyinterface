import numpy as np
import asyncio
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter, ListParameter
from horibacontroller import HoribaController

class HoribaSpectrumProcedure(Procedure):
    excitation_wavelength = FloatParameter("Excitation Wavelength", units = 'nm', default = 532 )
    center_wavelength = FloatParameter("Center Wavelength", units = 'nm', default=780)
    exposure         = IntegerParameter("Exposure", units = 'ms', default=1000)
    grating          = IntegerParameter("Grating",          default=3)
    # slit             = IntegerParameter("Slit", default = 1) //hardcoded slit, mirror selections
    slit_position    = FloatParameter("Slit position",  units = 'mm',        default=0.1)
    # mirror = IntegerParameter("Mirror", default = 1)
    mirror_position  = IntegerParameter("Mirror position",       default=0)
    gain             = IntegerParameter("Gain",              default=0) # list: ultimate sensititvity, high sensitivity, best dynamic range, high light
    speed            = IntegerParameter("Speed",            default=2) #list: 50khz, 1mhz, 3mhz

    DATA_COLUMNS = ["Wavenumber", "Intensity", "Wavelength"]

    def execute(self):
        params = {
        'center_wavelength': self.center_wavelength,
        'exposure': self.exposure,
        'grating': self.grating,
        #'slit': self.slit,
        'slit_position': self.slit_position,
        #'mirror': self.mirror,
        'mirror_position': self.mirror_position,
        'gain': self.gain,
        'speed': self.speed,
    }
        
        self.controller = HoribaController()
        x_data, y_data = asyncio.run(self.controller.initialize(**params))
        
        for i in range(len(y_data)):
            for x, y in zip(x_data, y_data[i]):
                self.emit("results", {
                    "Wavelength": x, #change to shift from initial lambda
                    "Wavenumber": (1/self.excitation_wavelength - 1 /x) * 1e7,
                    "Intensity": y
                })