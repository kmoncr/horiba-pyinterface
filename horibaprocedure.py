import numpy as np
import asyncio
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter
from horibacontroller import HoribaController

class HoribaSpectrumProcedure(Procedure):
    start_wavelength = FloatParameter("Start Wavelength (nm)", default=300)
    end_wavelength   = FloatParameter("End Wavelength (nm)", default=800)
    step_size        = FloatParameter("Step Size (nm)",    default=1)
    exposure         = IntegerParameter("Exposure (ms)",    default=500)
    grating          = IntegerParameter("Grating",          default=1)
    slit_position    = FloatParameter("Slit (mm)",          default=0.5)
    mirror_position  = IntegerParameter("Mirror Pos",       default=1)
    ' gain             = ListParameter("Gain", gain_labels, default=gain_labels[0])'
    gain             = IntegerParameter("Gain",             default=0)
    speed            = IntegerParameter("Speed",            default=2)

    DATA_COLUMNS = ["Wavenumber", "Intensity"]

    '''def startup(self):
        self.controller = HoribaController()
        asyncio.run(self.controller.initialize())
        print("startup complete")
        '''
        
    '''try:
            controller = HoribaController()
            gains_dict = asyncio.run(controller.get_available_gains())
        except Exception as e:
            print(f"failed to get gains: {e}")
            gains_dict = {}

        gain_labels = list(gains_dict.values())'''

    def execute(self):
        self.controller = HoribaController()
        x_data, y_data = asyncio.run(self.controller.initialize())
        
        for i in range(len(y_data)):
            for x, y in zip(x_data, y_data[i]):
                self.emit("results", {
                    "Wavenumber": 1/x,
                    "Intensity": y
                })