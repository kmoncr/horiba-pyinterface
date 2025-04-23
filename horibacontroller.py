import asyncio
import traceback
from typing import Any
from horiba_sdk.devices.device_manager import DeviceManager
from horiba_sdk.devices.single_devices import ChargeCoupledDevice, Monochromator
from horiba_sdk.core.timer_resolution import TimerResolution
from horiba_sdk.core.acquisition_format import AcquisitionFormat
from horiba_sdk.core.x_axis_conversion_type import XAxisConversionType
import matplotlib.pyplot as plt
import numpy as np

class HoribaController:
    def __init__(self,
                 start_icl: bool = True,
                 icl_ip: str = '127.0.0.1',
                 icl_port: str = '25010',
                 enable_binary: bool = True,
                 enable_logging: bool = False):
        self._dm = DeviceManager(
            start_icl=start_icl,
            icl_ip=icl_ip,
            icl_port=icl_port,
            enable_binary_messages=enable_binary,
            enable_logging=enable_logging
        )
        self.mono: Monochromator | None = None
        self.ccd: ChargeCoupledDevice | None = None

    async def initialize(self) -> tuple[dict, dict]:
        await self._dm.start()
        monos = self._dm.monochromators
        ccds  = self._dm.charge_coupled_devices
        if not monos or not ccds:
            raise RuntimeError("no monochromator or ccd found")
        
        self.mono = monos[0]
        await self.mono.open()
        await self._wait_for_mono()
       
        self.ccd  = ccds[0]
        await self.ccd.open()
        await self._wait_for_ccd()

        await self.mono.initialize()
        print("mono initialized")
        await self._wait_for_mono()
        await self.mono.set_turret_grating(Monochromator.Grating.THIRD)
        await self._wait_for_mono()

        await self.mono.move_to_target_wavelength(780)
        print("mono moved")
        await self._wait_for_mono()
        await self.mono.set_slit_position(self.mono.Slit.A, 0.1)    
        print("grating")
        await self.mono.set_mirror_position(self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL)
        print("mirror")
        await self._wait_for_mono()
        mono_wavelength = await self.mono.get_current_wavelength()
        print("wavelength", mono_wavelength)
        
        cfg = await self.ccd.get_configuration()
        print ("cfg reached")
        chip_x = int(cfg['chipWidth'])
        chip_y = int(cfg['chipHeight'])
        print("chips: ", chip_x, chip_y)
        await self.ccd.set_acquisition_count(1)
        print("acq count")
        await self.ccd.set_center_wavelength(self.mono.id(), 780)           
        await self.ccd.set_exposure_time(1000)
        await self.ccd.set_gain(0)  # High Light
        await self.ccd.set_speed(2)  # 1 MHz Ultra           
        await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
        print("timer res")
        await self.ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
        print("acq format")
        await self.ccd.set_region_of_interest(
         1, 0, 0, chip_x, chip_y, 1, chip_y
        )
        await self.ccd.set_x_axis_conversion_type(XAxisConversionType.FROM_ICL_SETTINGS_INI)
        print("x axis conv")

        ready = await self.ccd.get_acquisition_ready()
        if not ready:
            raise RuntimeError("ccd not ready for acquisition")
        await self.ccd.acquisition_start(open_shutter=True)
        await asyncio.sleep(1) 
        await self._wait_for_ccd()
        
        raw = await self.ccd.get_acquisition_data()
        print("spectrum acquired")
        x_data = raw[0]['roi'][0]['xData']
        y_data = raw[0]['roi'][0]['yData']
        await self.shutdown()

        return x_data, y_data
        
    async def _wait_for_mono(self) -> None:
        if self.mono is None:
            return
        busy = True
        while busy:
            busy = await self.mono.is_busy()
            await asyncio.sleep(0.1)

    async def _wait_for_ccd(self) -> None:
        if self.ccd is None:
            return
        busy = True
        while busy:
            busy = await self.ccd.get_acquisition_busy()
            await asyncio.sleep(0.1)

    async def set_grating(self, grating: Monochromator.Grating) -> None:
        await self.mono.set_turret_grating(grating)
        await self._wait_for_mono()

    async def set_target_wavelength(self, wavelength_nm: float) -> None:
        await self.mono.move_to_target_wavelength(wavelength_nm)
        await self._wait_for_mono()

    async def set_slit_position(self,
                                slit: Monochromator.Slit,
                                position: float) -> None:
        await self.mono.set_slit_position(slit, position)
        await self._wait_for_mono()

    async def set_mirror_position(self,
                                  mirror: Monochromator.Mirror,
                                  position: Monochromator.MirrorPosition) -> None:
        await self.mono.set_mirror_position(mirror, position)
        await self._wait_for_mono()

    async def get_available_gains(self) -> dict[int, str]:
        cfg = await self.ccd.get_configuration()
        return {g['token']: g['info'] for g in cfg['gains']}

    async def set_gain(self, gain: int) -> None:
        await self.ccd.set_gain(gain)

    async def set_speed(self, speed: int) -> None:
        await self.ccd.set_speed(speed)

    async def set_exposure_time(self, exposure_ms: int) -> None:
        await self.ccd.set_exposure_time(exposure_ms)

    async def acquire_spectrum(self) -> dict[Any, Any]:
        ready = await self.ccd.get_acquisition_ready()
        if not ready:
            raise RuntimeError("ccd not ready for acquisition")
        await self.ccd.acquisition_start(open_shutter=True)
        await asyncio.sleep(1) 
        await self._wait_for_ccd()
        
        raw = await self.ccd.get_acquisition_data()
        x = raw[0]['roi'][0]['xData']
        y = raw[0]['roi'][0]['yData']
        return x, y

    async def shutdown(self) -> None:
        if self.ccd is not None:
            await self.ccd.close()
        await asyncio.sleep(0.5)
        if self.mono is not None:
            await self.mono.close()
        await self._dm.stop()

    async def plot_values(self, target_wavelength, x_data, y_data):
    # Plotting the data
        if len(y_data) == 1:
            plt.plot(x_data, y_data[0], linestyle='-')
            plt.title(f'Wavelength ({target_wavelength}[nm]) vs. Intensity')
            plt.xlabel('Wavelength')
            plt.ylabel('Intensity')
            plt.grid(True)
            plt.show()
        else:
            arr = np.array(y_data)
            plt.imshow(arr, interpolation='nearest', aspect='auto')
            plt.show()