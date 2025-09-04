import asyncio
from typing import Any
from loguru import logger
from horiba_sdk.devices.device_manager import DeviceManager
from horiba_sdk.devices.single_devices import ChargeCoupledDevice, Monochromator
from horiba_sdk.core.timer_resolution import TimerResolution
from horiba_sdk.core.acquisition_format import AcquisitionFormat
from horiba_sdk.core.x_axis_conversion_type import XAxisConversionType


class HoribaController:
    def __init__(
        self,
        start_icl: bool = True,
        icl_ip: str = "127.0.0.1",
        icl_port: str = "25010",
        enable_binary: bool = True,
        enable_logging: bool = False,
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
        self.prev_center_wavelength = None
        self.prev_exposure = None
        self.prev_grating = None
        self.prev_slit_position = None
        self.prev_gain = None
        self.prev_speed = None

    async def initialize(self, **kwargs):
        center_wavelength = kwargs.get("center_wavelength", 780)
        exposure = kwargs.get("exposure", 1)
        grating = kwargs.get("grating", "Monochromator.Grating.THIRD")
        slit_position = kwargs.get("slit_position", 0.1)
        gain = kwargs.get("gain", 0)
        speed = kwargs.get("speed", 2)

        try:
            await self._dm.start()
            monos = self._dm.monochromators
            ccds = self._dm.charge_coupled_devices
            if not monos or not ccds:
                    logger.critical("no monochromator or ccd found")
                    raise RuntimeError("no mono or ccd found")

            self.mono = monos[0]
            self.ccd = ccds[0]

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

            if grating != self.prev_grating:
                await self.mono.set_turret_grating(grating)
                logger.debug(f"monochromator grating set to {grating}")
                await self._wait_for_mono()
                self.prev_grating = grating

            if center_wavelength != self.prev_center_wavelength:
                await self.mono.move_to_target_wavelength(center_wavelength)
                logger.debug(f"monochromator wavelength set to {center_wavelength} nm")
                await self._wait_for_mono()

            if slit_position != self.prev_slit_position:
                await self.mono.set_slit_position(self.mono.Slit.A, slit_position)
                logger.debug(f"monochromator slit position set to {slit_position}")
                await self._wait_for_mono()
                self.prev_slit_position = slit_position

            await self.mono.set_mirror_position(self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL)
            logger.debug("mirror position set")
            await self._wait_for_mono()
            mono_wavelength = await self.mono.get_current_wavelength()
            logger.info(f"final wavelength position: {mono_wavelength:.3f} nm")

            cfg = await self.ccd.get_configuration()
            await self._wait_for_ccd()
            logger.debug("getting config")
            chip_x = int(cfg['chipWidth'])
            chip_y = int(cfg['chipHeight'])
            logger.debug(f"ccd dimensions: {chip_x=} {chip_y=}")

            await self.ccd.set_acquisition_count(1)
            await self._wait_for_ccd()
            if center_wavelength != self.prev_center_wavelength:
                await self.ccd.set_center_wavelength(self.mono.id(), center_wavelength)   
                await self._wait_for_ccd()
                self.prev_center_wavelength = center_wavelength
                logger.debug("ccd center wavelength set")

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
            
            await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
            await self._wait_for_ccd()
            await self.ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
            await self._wait_for_ccd()
            await self.ccd.set_region_of_interest(1, 0, 0, chip_x, chip_y, 1, chip_y)
            await self._wait_for_ccd()
            await self.ccd.set_x_axis_conversion_type(XAxisConversionType.FROM_ICL_SETTINGS_INI)
            await self._wait_for_ccd()
            logger.debug("ccd settings complete")

            logger.debug("initialization complete")
            return True

        except Exception as e:
            logger.error(f"Error during initialization: {str(e)}")
            return False

    async def acquire_spectrum(self, **kwargs) -> dict[Any, Any]:

        logger.debug("starting acqn")
        if not await self.initialize(**kwargs):
            logger.error("failed initialization")
            raise RuntimeError("failed initialization")

        else:
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

            await self.shutdown()

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
        cfg = await self.ccd.get_configuration()
        return {g["token"]: g["info"] for g in cfg["gains"]}

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
        if self.ccd is not None:
            await self.ccd.close()
        await asyncio.sleep(0.5)
        if self.mono is not None:
            await self.mono.close()
        await self._dm.stop()
        logger.success("devices shut down")
