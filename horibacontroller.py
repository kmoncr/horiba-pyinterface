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

        dm = DeviceManager(start_icl=True)
        await dm.start()

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
            monos = dm.monochromators
            ccds = dm.charge_coupled_devices

            if not monos or not ccds:
                logger.critical("no monochromator or ccd found")
                await dm.stop()
                raise RuntimeError("no mono or ccd found")

            mono = monos[0]
            logger.debug("opening mono")
            await mono.open()
            await self._wait_for_mono(mono)

            ccd = ccds[0]
            logger.debug("opening ccd")
            await ccd.open()
            await self._wait_for_ccd(ccd)

            if not await mono.is_initialized():
                await mono.initialize()
                logger.debug("initializing mono")
                await self._wait_for_mono(mono)

            '''current_grating = await mono.get_turret_grating()'''
            logger.debug(f"Setting grating to {grating}")
            await mono.set_turret_grating(grating)
            await self._wait_for_mono(mono)

            logger.debug(f"Setting wavelength to {center_wavelength} nm")
            await mono.move_to_target_wavelength(center_wavelength)
            await self._wait_for_mono(mono)

            logger.debug(f"Setting slit position to {slit_position} mm")
            await mono.set_slit_position(mono.Slit.A, slit_position)
            await self._wait_for_mono(mono)

            await mono.set_mirror_position(
                mono.Mirror.ENTRANCE, mono.MirrorPosition.AXIAL
            )
            logger.debug("mirror position set")
            await self._wait_for_mono(mono)

            mono_wavelength = await mono.get_current_wavelength()
            logger.info(f"Final wavelength position: {mono_wavelength:.3f} nm")

            cfg = await ccd.get_configuration()
            chip_x = int(cfg["chipWidth"])
            chip_y = int(cfg["chipHeight"])
            logger.debug(f"ccd dimensions: {chip_x=} {chip_y=}")

            logger.debug("Configuring CCD parameters")
            await ccd.set_acquisition_count(1)
            await ccd.set_center_wavelength(mono.id(), center_wavelength)
            await ccd.set_exposure_time(int(exposure * 1000))
            await ccd.set_gain(gain)
            await ccd.set_speed(speed)
            await ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
            await ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
            await ccd.set_region_of_interest(1, 0, 0, chip_x, chip_y, 1, chip_y)
            await ccd.set_x_axis_conversion_type(
                XAxisConversionType.FROM_ICL_SETTINGS_INI
            )

            ready = await ccd.get_acquisition_ready()
            if not ready:
                logger.error("CCD not ready for acquisition")
                raise RuntimeError("CCD not ready for acquisition")

            logger.info("Starting acquisition...")
            await ccd.acquisition_start(open_shutter=True)
            await asyncio.sleep(1)
            await self._wait_for_ccd(ccd)

            raw = await ccd.get_acquisition_data()
            logger.success("Spectrum acquired successfully")

            x = raw[0]["roi"][0]["xData"]
            y = raw[0]["roi"][0]["yData"]

            await ccd.close()
            await asyncio.sleep(0.5)
            await mono.close()
            await dm.stop()

            return x, y

        except Exception as e:
            logger.exception("Failed to acquire spectrum")
            try:
                await dm.stop()
            except:
                pass
            raise

    async def _wait_for_mono(self, mono: Monochromator) -> None:
        mono_busy = True
        while mono_busy:
            mono_busy = await mono.is_busy()
            await asyncio.sleep(1)
            logger.info("Mono busy...")

    async def _wait_for_ccd(self, ccd: ChargeCoupledDevice) -> None:
        busy = True
        while busy:
            busy = await ccd.get_acquisition_busy()
            await asyncio.sleep(0.1)

    async def set_rotation_angle(self, value: float) -> None:
        """Set rotation stage angle"""
        if not self.enable_rotation_stage or self.rotation_stage is None:
            logger.error("Rotation stage not enabled")
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
            logger.error("Rotation stage not enabled")
            return
        if not self.rotation_stage.is_connected:
            logger.error("Rotation stage not connected")
            return
        self.rotation_stage.return_to_origin()

    async def shutdown(self) -> None:
        logger.info("disconnecting rotation stage")
        if self.enable_rotation_stage and self.rotation_stage:
            try:
                self.rotation_stage.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting rotation stage: {e}")
        logger.success("shutdown complete")
