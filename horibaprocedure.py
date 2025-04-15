import numpy as np
import asyncio
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter, ListParameter
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

    DATA_COLUMNS = ["Wavenumber (cm^-1)", "Intensity"]

    def startup(self):
        self.controller = HoribaController()
        asyncio.run(self.controller.initialize())
        self.aborted = False
        
        '''try:
            controller = HoribaController()
            gains_dict = asyncio.run(controller.get_available_gains())
        except Exception as e:
            print(f"failed to get gains: {e}")
            gains_dict = {}

        gain_labels = list(gains_dict.values())'''

    def execute(self):
        asyncio.run(self._run_scan())

    async def _run_scan(self):
        await self.controller.set_grating(self.grating)
        await self.controller.set_slit_position(self.slit_position)
        await self.controller.set_mirror_position(self.mirror_position,
                                                   Monochromator.MirrorPosition.AXIAL)
        await self.controller.set_gain(gain_token)
        await self.controller.set_speed(self.speed)

        wavelengths = np.arange(self.start_wavelength,
                                self.end_wavelength,
                                self.step_size)
        for wl in wavelengths:
            if self.aborted:
                break
            wavenumber = 1 / wl
            await self.controller.set_target_wavelength(wl)
            x, y = await self.controller.acquire_spectrum(self.exposure)
            for xi, yi in zip(x, y):
                self.emit("results", {"Wavenumber (cm^-1)": wavenumber, "Intensity": yi})

    def shutdown(self):
        asyncio.run(self.controller.shutdown())

    def abort(self):
        self.aborted = True
        asyncio.run(self.controller.shutdown())