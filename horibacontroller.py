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
        
        self.prev_center_wavelength = None
        self.prev_exposure = None
        self.prev_grating = None
        self.prev_slit_position = None
        self.prev_gain = None
        self.prev_speed = None
        self.prev_rotation_angle = None

    async def _ensure_initialized(self):
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

    async def _configure_for_acquisition(self, **kwargs):
        center_wavelength = kwargs.get("center_wavelength", 780)
        exposure = kwargs.get("exposure", 1)
        grating = kwargs.get("grating")
        slit_position = kwargs.get("slit_position", 0.1)
        gain = kwargs.get("gain", 0)
        speed = kwargs.get("speed", 2)
        rotation_angle = kwargs.get("rotation_angle", None)  

        if grating != self.prev_grating:
            logger.debug(grating)
            await self.mono.set_turret_grating(grating)
            logger.debug(f"monochromator grating set to {grating}")
            await self._wait_for_mono()
            self.prev_grating = grating

        if center_wavelength != self.prev_center_wavelength:
            await self.mono.move_to_target_wavelength(center_wavelength)
            logger.debug(f"monochromator wavelength set to {center_wavelength} nm")
            await self._wait_for_mono()
            
            await self.ccd.set_center_wavelength(self.mono.id(), center_wavelength)   
            await self._wait_for_ccd()
            self.prev_center_wavelength = center_wavelength
            logger.debug("ccd center wavelength set")
            await self.ccd.set_x_axis_conversion_type(XAxisConversionType.FROM_ICL_SETTINGS_INI)
            await self._wait_for_ccd()

        if slit_position != self.prev_slit_position:
            await self.mono.set_slit_position(self.mono.Slit.A, slit_position)
            logger.debug(f"monochromator slit position set to {slit_position}")
            await self._wait_for_mono()
            self.prev_slit_position = slit_position

        if exposure != self.prev_exposure:
            await self.ccd.set_exposure_time(exposure * 1e3)  # convert s to ms
            await self._wait_for_ccd()
            logger.debug(f"CCD exposure time set to {exposure} ms")
            self.prev_exposure = exposure

        if gain != self.prev_gain:
            await self.ccd.set_gain(gain)
            await self._wait_for_ccd()
            logger.debug(f"CCD gain set to {gain}")
            self.prev_gain = gain

        if speed != self.prev_speed:
            await self.ccd.set_speed(speed)
            await self._wait_for_ccd()
            logger.debug(f"CCD speed set to {speed}")
            self.prev_speed = speed
        
        if (rotation_angle is not None and 
            self.enable_rotation_stage and 
            self.rotation_stage and 
            self.rotation_stage.is_connected and
            rotation_angle != self.prev_rotation_angle):
            
            logger.debug(f"Setting rotation stage to {rotation_angle} degrees")
            self.rotation_stage.degree = rotation_angle
            self.prev_rotation_angle = rotation_angle
            logger.info(f"Rotation stage set to {rotation_angle} degrees")
        
        mono_wavelength = await self.mono.get_current_wavelength()
        logger.info(f"final wavelength position: {mono_wavelength:.3f} nm")

    async def acquire_spectrum(self, **kwargs) -> tuple[Any, Any]:
        logger.debug("starting acquisition")
        
        if not await self._ensure_initialized():
            logger.error("failed initialization")
            raise RuntimeError("failed initialization")

        await self._configure_for_acquisition(**kwargs)

        ready = await self.ccd.get_acquisition_ready()
        if not ready:
            logger.critical("ccd not ready for acquisition")
            raise RuntimeError("ccd not ready for acquisition")

        await self.ccd.acquisition_start(open_shutter=True)
        await asyncio.sleep(1)
        await self._wait_for_ccd()

        raw = await self.ccd.get_acquisition_data()
        logger.success("spectrum acquired")
        x = raw[0]["roi"][0]["xData"]
        y = raw[0]["roi"][0]["yData"]

        return x, y

    async def _wait_for_mono(self) -> None:
        if self.mono is None:
            return
        busy = True
        while busy:
            busy = await self.mono.is_busy()
            await asyncio.sleep(0.1)
            logger.info("Mono busy...")

    async def _wait_for_ccd(self) -> None:
        if self.ccd is None:
            return
        busy = True
        while busy:
            busy = await self.ccd.get_acquisition_busy()
            await asyncio.sleep(0.1)
            logger.info("Acquisition busy")

    async def set_grating(self, grating: Monochromator.Grating) -> None:
        logger.debug("setting grating: {}", grating)
        await self.mono.set_turret_grating(grating)
        await self._wait_for_mono()

    async def set_target_wavelength(self, wavelength_nm: float) -> None:
        logger.debug("setting target wavelength: {}", wavelength_nm)
        await self.mono.move_to_target_wavelength(wavelength_nm)
        await self._wait_for_mono()

    async def set_slit_position(
        self, slit: Monochromator.Slit, position: float
    ) -> None:
        logger.debug("setting slit {} to position {}", slit, position)
        await self.mono.set_slit_position(slit, position)
        await self._wait_for_mono()

    async def set_mirror_position(
        self, mirror: Monochromator.Mirror, position: Monochromator.MirrorPosition
    ) -> None:
        logger.debug("setting mirror {} to position {}", mirror, position)
        await self.mono.set_mirror_position(mirror, position)
        await self._wait_for_mono()

    async def get_available_gains(self) -> dict[int, str]:
        await self._ensure_initialized()
        cfg = await self.ccd.get_configuration()
        return {g["token"]: g["info"] for g in cfg["gains"]}

    def set_rotation_angle(self, angle: float) -> None:
        if not self.enable_rotation_stage or not self.rotation_stage:
            logger.warning("Rotation stage not enabled")
            return
        
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
            
        logger.debug(f"Setting rotation angle to {angle} degrees")
        self.rotation_stage.degree = angle

    def get_rotation_angle(self) -> float:
        if not self.enable_rotation_stage or not self.rotation_stage:
            return 0.0
        
        if not self.rotation_stage.is_connected:
            return 0.0
            
        return self.rotation_stage.degree

    def return_rotation_to_origin(self) -> None:
        if not self.enable_rotation_stage or not self.rotation_stage:
            logger.warning("Rotation stage not enabled")
            return
            
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
            
        self.rotation_stage.return_to_origin()

    def get_rotation_status(self) -> dict:
        if not self.enable_rotation_stage or not self.rotation_stage:
            return {"enabled": False}
        
        return self.rotation_stage.get_status()

    async def set_gain(self, gain: int) -> None:
        logger.debug("setting gain: {}", gain)
        await self.ccd.set_gain(gain)

    async def set_speed(self, speed: int) -> None:
        logger.debug("setting speed: {}", speed)
        await self.ccd.set_speed(speed)

    async def set_exposure_time(self, exposure_ms: int) -> None:
        logger.debug("setting exposure time: {}", exposure_ms)
        await self.ccd.set_exposure_time(exposure_ms)

    async def shutdown(self) -> None:
        logger.info("shutting down devices")
        
        if self.enable_rotation_stage and self.rotation_stage:
            self.rotation_stage.disconnect()
            
        if self.ccd is not None and self._devices_opened:
            await self.ccd.close()
        await asyncio.sleep(0.5)
        if self.mono is not None and self._devices_opened:
            await self.mono.close()
        if self._is_initialized:
            await self._dm.stop()
        logger.success("devices shut down")
        
    def __del__(self):
        if self._is_initialized:
            try:
                asyncio.create_task(self.shutdown())
            except:
                pass