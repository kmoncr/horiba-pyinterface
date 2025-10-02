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
        start_icl: bool = True,
        icl_ip: str = "127.0.0.1",
        icl_port: str = "25010",
        enable_binary: bool = True,
        enable_logging: bool = True,
        rotation_stage_port: str = "COM3",
        enable_rotation_stage: bool = True,
    ):
        logger.debug("initializing device manager with ip={} port={}", icl_ip, icl_port)
        self._dm = DeviceManager(
            start_icl=start_icl,
            icl_ip=icl_ip,
            icl_port=icl_port,
            enable_binary_messages=enable_binary,
            enable_logging=enable_logging,
        )
        if not enable_logging:
            logger.remove()
        
        self.mono: Monochromator | None = None
        self.ccd: ChargeCoupledDevice | None = None
        self._is_initialized = False
        self._devices_opened = False
        
        self.rotation_stage: OptoSigmaController | None = None
        self.enable_rotation_stage = enable_rotation_stage
        if enable_rotation_stage:
            self.rotation_stage = OptoSigmaController(port=rotation_stage_port)

    async def initialize(self, **kwargs):
        """Public initialization method"""
        return await self._ensure_initialized()

    async def _ensure_initialized(self):
        """Private initialization implementation"""
        if self._is_initialized:
            return True
            
        try:
            await self._dm.start()
            monos = self._dm.monochromators
            ccds = self._dm.charge_coupled_devices
            
            if not monos or not ccds:
                logger.critical("no monochromator or ccd found")
                raise RuntimeError("no mono or ccd found")

            self.mono = monos[0]
            self.ccd = ccds[0]

            if not self._devices_opened:
                if not await self.mono.is_open():
                    logger.debug("opening mono")
                    await self.mono.open()
                    await self._wait_for_mono()

                if not await self.ccd.is_open():
                    logger.debug("initializing ccd")
                    await self.ccd.open()
                    await self._wait_for_ccd()

                if not await self.mono.is_initialized():
                    await self.mono.initialize()
                    logger.debug("monochromator initialized")
                    await self._wait_for_mono()

                await self.mono.set_mirror_position(self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL)
                logger.debug("mirror position set")
                await self._wait_for_mono()
                
                cfg = await self.ccd.get_configuration()
                await self._wait_for_ccd()
                logger.debug("getting config")
                chip_x = int(cfg['chipWidth'])
                chip_y = int(cfg['chipHeight'])
                logger.debug(f"ccd dimensions: {chip_x=} {chip_y=}")

                await self.ccd.set_acquisition_count(1)
                await self._wait_for_ccd()
                await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
                await self._wait_for_ccd()
                await self.ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
                await self._wait_for_ccd()
                await self.ccd.set_region_of_interest(1, 0, 0, chip_x, chip_y, 1, chip_y)
                await self._wait_for_ccd()
                
                self._devices_opened = True
                logger.debug("devices opened and configured")

            if self.enable_rotation_stage and self.rotation_stage:
                if not self.rotation_stage.is_connected:
                    if self.rotation_stage.connect():
                        logger.info("Rotation stage connected")
                    else:
                        logger.warning("Failed to connect rotation stage - continuing without it")

            self._is_initialized = True
            logger.debug("initialization complete")
            return True

        except Exception as e:
            logger.error(f"Error during initialization: {str(e)}")
            self._is_initialized = False
            return False

    async def acquire_spectrum(self, **kwargs) -> tuple[Any, Any]:
    
        logger.info("Starting acquisition with parameters:")
        for k, v in kwargs.items():
            logger.info(f"  {k}: {v}")
    
        center_wavelength = kwargs.get("center_wavelength", 780)
        exposure = kwargs.get("exposure", 1)
        grating = kwargs.get("grating")
        slit_position = kwargs.get("slit_position", 0.1)
        gain = kwargs.get("gain", 0)
        speed = kwargs.get("speed", 2)
        rotation_angle = kwargs.get("rotation_angle", None)
    
        if grating is not None:
            logger.debug(f"Setting grating to {grating}")
            await self.mono.set_turret_grating(grating)
            await self._wait_for_mono()
    
        logger.debug(f"Setting wavelength to {center_wavelength} nm")
        await self.mono.move_to_target_wavelength(center_wavelength)
        await self._wait_for_mono()
    
        await self.ccd.set_center_wavelength(self.mono.id(), center_wavelength)
        await self._wait_for_ccd()
    
        await self.ccd.set_x_axis_conversion_type(XAxisConversionType.FROM_ICL_SETTINGS_INI)
        await self._wait_for_ccd()
    
        logger.debug(f"Setting slit position to {slit_position}")
        await self.mono.set_slit_position(self.mono.Slit.A, slit_position)
        await self._wait_for_mono()
    
        logger.debug(f"Setting exposure to {exposure} s")
        await self.ccd.set_exposure_time(exposure * 1e3)  # convert s to ms
        await self._wait_for_ccd()
    
        logger.debug(f"Setting gain to {gain}")
        await self.ccd.set_gain(gain)
        await self._wait_for_ccd()

        logger.debug(f"Setting speed to {speed}")
        await self.ccd.set_speed(speed)
        await self._wait_for_ccd()
    
        if (rotation_angle is not None and 
            self.enable_rotation_stage and 
            self.rotation_stage and 
            self.rotation_stage.is_connected):
            logger.debug(f"Setting rotation to {rotation_angle} degrees")
            self.rotation_stage.degree = rotation_angle
            logger.info(f"Rotation stage set to {rotation_angle} degrees")
    
        mono_wavelength = await self.mono.get_current_wavelength()
        logger.info(f"Final wavelength position: {mono_wavelength:.3f} nm")
    
        try:
            ready = await self.ccd.get_acquisition_ready()
            if not ready:
                logger.error("CCD not ready for acquisition")
                raise RuntimeError("CCD not ready for acquisition")

            await self.ccd.acquisition_start(open_shutter=True)
            await asyncio.sleep(0.2)
            await self._wait_for_ccd()

            raw = await self.ccd.get_acquisition_data()
            logger.success("Spectrum acquired successfully")

            x = raw[0]["roi"][0].get("xData")
            y = raw[0]["roi"][0].get("yData")

            if isinstance(x, list) and len(x) == 1:
             x = x[0]
            if isinstance(y, list) and len(y) == 1:
              y = y[0]

            return x, y
        
        except Exception as e:
            logger.exception("Failed to acquire spectrum")
            raise

    async def _wait_for_mono(self) -> None:
        if self.mono is None:
            return
        await asyncio.sleep(0.1) 
        busy = True
        timeout = 5  
        elapsed = 0
        while busy and elapsed < timeout:
            busy = await self.mono.is_busy()
            await asyncio.sleep(0.1)
            elapsed += 0.1
        if elapsed >= timeout:
            logger.warning("Mono busy timeout - continuing anyway")

    async def _wait_for_ccd(self) -> None:
        if self.ccd is None:
            return
        busy = True
        while busy:
            busy = await self.ccd.get_acquisition_busy()
            await asyncio.sleep(0.1)

    async def get_available_gains(self) -> dict[int, str]:
        await self._ensure_initialized()
        cfg = await self.ccd.get_configuration()
        return {g["token"]: g["info"] for g in cfg["gains"]}

    async def set_rotation_angle(self, value: float) -> None:
        """Set rotation stage angle"""
        if not self.enable_rotation_stage or self.rotation_stage is None:
            logger.error("Rotation stage not connected")
            return
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
        self.rotation_stage.degree = value

    async def get_rotation_angle(self) -> float:
        """Get current rotation stage angle"""
        if not self.enable_rotation_stage or self.rotation_stage is None:
            return 0.0
        if not self.rotation_stage.is_connected:
            return 0.0
        return self.rotation_stage.degree

    async def return_rotation_to_origin(self) -> None:
        """Return rotation stage to origin"""
        if not self.enable_rotation_stage or self.rotation_stage is None:
            logger.error("Rotation stage not connected")
            return
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
        self.rotation_stage.return_to_origin()

    async def shutdown(self) -> None:
        logger.info("shutting down devices")

        if self.enable_rotation_stage and self.rotation_stage:
            try:
                self.rotation_stage.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting rotation stage: {e}")
    
        if self.ccd is not None and self._devices_opened:
            try:
                await self.ccd.close()
            except Exception as e:
                logger.error(f"Error closing CCD: {e}")
    
        await asyncio.sleep(0.5)
    
        if self.mono is not None and self._devices_opened:
            try:
                await self.mono.close()
            except Exception as e:
                logger.error(f"Error closing mono: {e}")
    
        if self._is_initialized:
            try:
                await self._dm.stop()
            except Exception as e:
                logger.error(f"Error stopping device manager: {e}")
    
    logger.success("devices shut down")
    def __del__(self):
        if self._is_initialized:
            try:
                asyncio.create_task(self.shutdown())
            except:
                pass