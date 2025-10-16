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

        self._dm = DeviceManager(start_icl=True, enable_logging=enable_logging)
        self.mono = None
        self.ccd = None

        self.rotation_stage: OptoSigmaController | None = None
        self.enable_rotation_stage = enable_rotation_stage
        if enable_rotation_stage:
            self.rotation_stage = OptoSigmaController(port=rotation_stage_port)
            if self.rotation_stage.connect():
                logger.info("Rotation stage connected")
            else:
                logger.warning(
                    "Failed to connect rotation stage - continuing without it"
                )

    async def _ensure_initialized(self) -> bool:
        try:
            if not self._dm.is_started:
                await self._dm.start()

            monos = self._dm.monochromators
            ccds = self._dm.charge_coupled_devices

            if not monos or not ccds:
                logger.critical("No monochromator or CCD found")
                raise RuntimeError("No mono or CCD found")

            self.mono = monos[0]
            self.ccd = ccds[0]

            if not await self.mono.is_open():
                await self.mono.open()
                await self._wait_for_mono(self.mono)

            if not await self.mono.is_initialized():
                await self.mono.initialize()
                await self._wait_for_mono(self.mono)

            if not await self.ccd.is_open():
                await self.ccd.open()
                await self._wait_for_ccd(self.ccd)

            return True

        except Exception as e:
            logger.exception("Device initialization failed")
            raise

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

        await self._ensure_initialized()

        if (
            rotation_angle is not None
            and self.enable_rotation_stage
            and self.rotation_stage
            and self.rotation_stage.is_connected
        ):
            logger.debug(f"Setting rotation to {rotation_angle} degrees")
            self.rotation_stage.degree = rotation_angle
            logger.info(f"Rotation stage set to {rotation_angle} degrees")

        try:
            if grating is not None:
                logger.debug(f"Setting grating to {grating}")
                await self.mono.set_turret_grating(grating)
                await self._wait_for_mono(self.mono)

            logger.debug(f"Setting wavelength to {center_wavelength} nm")
            await self.mono.move_to_target_wavelength(center_wavelength)
            await self._wait_for_mono(self.mono)

            logger.debug(f"Setting slit position to {slit_position} mm")
            await self.mono.set_slit_position(self.mono.Slit.A, slit_position)
            await self._wait_for_mono(self.mono)

            await self.mono.set_mirror_position(
                self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL
            )
            await self._wait_for_mono(self.mono)

            mono_wavelength = await self.mono.get_current_wavelength()
            logger.info(f"Final wavelength position: {mono_wavelength:.3f} nm")

            cfg = await self.ccd.get_configuration()
            chip_x = int(cfg["chipWidth"])
            chip_y = int(cfg["chipHeight"])
            logger.debug(f"CCD dimensions: {chip_x=} {chip_y=}")

            await self.ccd.set_acquisition_count(1)
            await self.ccd.set_center_wavelength(self.mono.id(), center_wavelength)
            await self.ccd.set_exposure_time(int(exposure * 1000))
            await self.ccd.set_gain(gain)
            await self.ccd.set_speed(speed)
            await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
            await self.ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
            await self.ccd.set_region_of_interest(1, 0, 0, chip_x, chip_y, 1, chip_y)
            await self.ccd.set_x_axis_conversion_type(
                XAxisConversionType.FROM_ICL_SETTINGS_INI
            )

            ready = await self.ccd.get_acquisition_ready()
            if not ready:
                raise RuntimeError("CCD not ready for acquisition")

            await self.ccd.acquisition_start(open_shutter=True)
            await asyncio.sleep(0.2)
            await self._wait_for_ccd(self.ccd)

            raw = await self.ccd.get_acquisition_data()
            logger.success("Spectrum acquired successfully")

            x = raw[0]["roi"][0]["xData"]
            y = raw[0]["roi"][0]["yData"]

            return x, y

        except Exception as e:
            logger.exception("Failed to acquire spectrum")
            raise

    async def _wait_for_mono(self, mono: Monochromator, timeout: float = 30.0) -> None:
        start_time = asyncio.get_event_loop().time()
        while True:
            mono_busy = await mono.is_busy()
            if not mono_busy:
                return
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError(
                    f"Monochromator busy wait timed out after {timeout}s"
                )
            await asyncio.sleep(1)
            logger.debug("Mono busy...")

    async def _wait_for_ccd(
        self, ccd: ChargeCoupledDevice, timeout: float = 30.0
    ) -> None:
        start_time = asyncio.get_event_loop().time()
        while True:
            busy = await ccd.get_acquisition_busy()
            if not busy:
                return
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError(f"CCD busy wait timed out after {timeout}s")
            await asyncio.sleep(0.1)

    async def set_rotation_angle(self, value: float) -> None:
        if not self.enable_rotation_stage or self.rotation_stage is None:
            logger.error("Rotation stage not enabled")
            return
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
        self.rotation_stage.degree = value

    async def get_rotation_angle(self) -> float:
        if not self.enable_rotation_stage or self.rotation_stage is None:
            return 0.0
        if not self.rotation_stage.is_connected:
            return 0.0
        return self.rotation_stage.degree

    async def return_rotation_to_origin(self) -> None:
        if not self.enable_rotation_stage or self.rotation_stage is None:
            logger.error("Rotation stage not enabled")
            return
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
        self.rotation_stage.return_to_origin()

    async def shutdown(self) -> None:
        try:
            if self.ccd:
                await self.ccd.close()
            if self.mono:
                await self.mono.close()
            await self._dm.stop()

            if self.enable_rotation_stage and self.rotation_stage:
                logger.info("Disconnecting rotation stage")
                try:
                    self.rotation_stage.disconnect()
                except Exception as e:
                    logger.error(f"Error disconnecting rotation stage: {e}")

            logger.success("Shutdown complete")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            raise
