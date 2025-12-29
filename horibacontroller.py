import asyncio
from typing import Any
from loguru import logger
from horiba_sdk.devices.device_manager import DeviceManager
from horiba_sdk.devices.single_devices import ChargeCoupledDevice, Monochromator
from horiba_sdk.core.timer_resolution import TimerResolution
from horiba_sdk.core.acquisition_format import AcquisitionFormat
from horiba_sdk.core.x_axis_conversion_type import XAxisConversionType
from optosigmacontroller import OptoSigmaController

class HoribaController:
    def __init__(
        self,
        enable_logging: bool = True,
        rotation_stage_port: str = "COM3",
        enable_rotation_stage: bool = True,
    ):
        if not enable_logging:
            logger.remove()

        self.dm = None
        self.mono = None
        self.ccd = None
        self.is_connected = False
        
        self._current_params = {
            'wavelength': None,
            'grating': None,
            'slit': None,
            'mirror': None
        }

        self.rotation_stage: OptoSigmaController | None = None
        self.enable_rotation_stage = enable_rotation_stage
        self.last_angle = 0.0

        if enable_rotation_stage:
            self.rotation_stage = OptoSigmaController(port=rotation_stage_port)
            if self.rotation_stage.connect():
                logger.info("Rotation stage connected")
                try:
                    self.last_angle = self.rotation_stage.degree
                except Exception as e:
                    logger.warning(f"Could not read initial angle: {e}")
            else:
                logger.warning("Failed to connect rotation stage - continuing without it")

    async def connect_hardware(self):
        """Initializes the connection to Horiba hardware once."""
        if self.is_connected:
            return

        logger.info("Initializing Horiba Hardware Connection...")

        if self.dm:
            try:
                await self.dm.stop()
            except Exception:
                pass
            self.dm = None

        self.dm = DeviceManager(start_icl=True)
        await self.dm.start()

        monos = self.dm.monochromators
        ccds = self.dm.charge_coupled_devices

        if not monos or not ccds:
            await self.dm.stop()
            raise RuntimeError("No mono or CCD found")

        self.mono = monos[0]
        self.ccd = ccds[0]

        await self.mono.open()
        await self._wait_for_mono(self.mono)
        await self.ccd.open()
        await self._wait_for_ccd(self.ccd)

        if not await self.mono.is_initialized():
            await self.mono.initialize()
            await self._wait_for_mono(self.mono)
        
        self.is_connected = True
        logger.success("Horiba Hardware Initialized and Ready.")

    async def acquire_spectrum(self, **kwargs) -> tuple[Any, Any]:
        if not self.is_connected:
            await self.connect_hardware()

        center_wavelength = kwargs.get("center_wavelength", 780)
        exposure = kwargs.get("exposure", 1)
        grating = kwargs.get("grating")
        slit_position = kwargs.get("slit_position", 0.1)
        gain = kwargs.get("gain", 0)
        speed = kwargs.get("speed", 2)
        rotation_angle = kwargs.get("rotation_angle", None)
        
        y_origin = kwargs.get("ccd_y_origin", 0)
        y_size = kwargs.get("ccd_y_size", 256)
        x_bin = kwargs.get("ccd_x_bin", 1)

        if rotation_angle is not None and self.enable_rotation_stage and self.rotation_stage:
            if abs(self.last_angle - rotation_angle) > 0.01: 
                self.rotation_stage.degree = rotation_angle
                self.last_angle = rotation_angle
                logger.info(f"Rotation angle set to: {rotation_angle}")

        try:
            if self._current_params['grating'] != grating:
                logger.debug(f"Setting grating to {grating}")
                await self.mono.set_turret_grating(grating)
                await self._wait_for_mono(self.mono)
                self._current_params['grating'] = grating

            if self._current_params['wavelength'] != center_wavelength:
                logger.debug(f"Moving to {center_wavelength} nm")
                await self.mono.move_to_target_wavelength(center_wavelength)
                await self._wait_for_mono(self.mono)
                self._current_params['wavelength'] = center_wavelength

            if self._current_params['slit'] != slit_position:
                logger.debug(f"Setting slit to {slit_position} mm")
                await self.mono.set_slit_position(self.mono.Slit.A, slit_position)
                await self._wait_for_mono(self.mono)
                self._current_params['slit'] = slit_position

            if self._current_params['mirror'] != 'AXIAL':
                await self.mono.set_mirror_position(self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL)
                await self._wait_for_mono(self.mono)
                self._current_params['mirror'] = 'AXIAL'

            cfg = await self.ccd.get_configuration()
            chip_x = int(cfg["chipWidth"])
            
            await self.ccd.set_acquisition_count(1)
            await self.ccd.set_center_wavelength(self.mono.id(), center_wavelength)
            await self.ccd.set_exposure_time(int(exposure * 1000))
            await self.ccd.set_gain(gain)
            await self.ccd.set_speed(speed)
            await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
            await self.ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
            
            await self.ccd.set_region_of_interest(1, 0, int(y_origin), chip_x, int(y_size), int(x_bin), int(y_size))
            await self.ccd.set_x_axis_conversion_type(XAxisConversionType.FROM_ICL_SETTINGS_INI)

            ready = await self.ccd.get_acquisition_ready()
            if not ready:
                raise RuntimeError("CCD not ready for acquisition")

            await self.ccd.acquisition_start(open_shutter=True)
            
            if exposure > 0.1:
                await asyncio.sleep(exposure * 0.9) 
            await self._wait_for_ccd(self.ccd)

            raw = await self.ccd.get_acquisition_data()
            x = raw[0]["roi"][0]["xData"]
            y = raw[0]["roi"][0]["yData"]

            return x, y

        except Exception as e:
            logger.exception("Failed to acquire spectrum - Connection might be lost")
            
            self.is_connected = False 
            
            try:
                if self.dm:
                    await self.dm.stop()
            except:
                pass
            self.dm = None 
            
            raise

    async def _wait_for_mono(self, mono: Monochromator) -> None:
        while await mono.is_busy():
            await asyncio.sleep(0.1)

    async def _wait_for_ccd(self, ccd: ChargeCoupledDevice) -> None:
        while await ccd.get_acquisition_busy():
            await asyncio.sleep(0.05)

    async def set_rotation_angle(self, value: float) -> None:
        if self.enable_rotation_stage and self.rotation_stage and self.rotation_stage.is_connected:
            self.rotation_stage.degree = value
            self.last_angle = value

    async def get_rotation_angle(self) -> float:
        if self.enable_rotation_stage and self.rotation_stage and self.rotation_stage.is_connected:
            self.last_angle = self.rotation_stage.degree
        return self.last_angle

    async def return_rotation_to_origin(self) -> None:
        if self.enable_rotation_stage and self.rotation_stage and self.rotation_stage.is_connected:
            self.rotation_stage.return_to_origin()
            self.last_angle = 0.0

    async def shutdown(self) -> None:
        logger.info("Shutting down hardware...")
        if self.enable_rotation_stage and self.rotation_stage:
            try:
                self.rotation_stage.disconnect()
            except:
                pass

        if self.is_connected:
            try:
                if self.ccd: await self.ccd.close()
                if self.mono: await self.mono.close()
                if self.dm: await self.dm.stop()
            except Exception as e:
                logger.error(f"Error closing Horiba devices: {e}")
            self.is_connected = False
        
        logger.success("Shutdown complete")