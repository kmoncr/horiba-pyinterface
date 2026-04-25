import asyncio
from typing import Any
from loguru import logger
from horiba_sdk.devices.device_manager import DeviceManager
from horiba_sdk.devices.single_devices import ChargeCoupledDevice, Monochromator
from horiba_sdk.core.timer_resolution import TimerResolution
from horiba_sdk.core.acquisition_format import AcquisitionFormat
from horiba_sdk.core.x_axis_conversion_type import XAxisConversionType
from optosigmacontroller import OptoSigmaController

try:
    from thorlabscontroller import ThorlabsK10CR2Controller
    _THORLABS_AVAILABLE = True
except ImportError:
    _THORLABS_AVAILABLE = False


class HoribaController:
    def __init__(
        self,
        enable_logging: bool = True,
        rotation_stage_port: str = "COM3",
        enable_rotation_stage: bool = True,
        thorlabs_serial: str = "55508504",
        enable_thorlabs_stage: bool = True,
    ):
        if not enable_logging:
            logger.remove()

        self.dm = None
        self.mono = None
        self.ccd = None
        self.is_connected = False

        # Guard flag: True while a scan is in progress.
        # Prevents temperature polls from hitting the CCD mid-acquisition.
        self._acquiring = False

        # Single asyncio.Lock serialising every SDK call against the
        # ICL websocket. Built lazily on first use because the lock must
        # belong to whichever event loop ends up driving the controller
        # (the GUI runs one in a background thread; tests use asyncio.run
        # which builds a fresh loop per call).
        self._sdk_lock: asyncio.Lock | None = None

        self._current_params = {
            'wavelength': None,
            'grating': None,
            'slit': None,
            'mirror': None
        }

        # ── OptoSigma rotation stage ─────────────────────────────────
        self.rotation_stage: OptoSigmaController | None = None
        self.enable_rotation_stage = enable_rotation_stage
        self.last_angle = 0.0

        if enable_rotation_stage:
            self.rotation_stage = OptoSigmaController(port=rotation_stage_port)
            if self.rotation_stage.connect():
                logger.info("OptoSigma rotation stage connected")
                try:
                    self.last_angle = self.rotation_stage.degree
                except Exception as e:
                    logger.warning(f"could not read initial OptoSigma angle: {e}")
            else:
                logger.warning("failed to connect to OptoSigma rotation stage")

        # ── Thorlabs K10CR2 rotation stage ────────────────────────────
        self.thorlabs_stage: ThorlabsK10CR2Controller | None = None
        self.enable_thorlabs_stage = False
        self.last_thorlabs_angle = 0.0

        if enable_thorlabs_stage and _THORLABS_AVAILABLE:
            try:
                stage = ThorlabsK10CR2Controller(serial_number=thorlabs_serial)
                if stage.connect():
                    self.thorlabs_stage = stage
                    self.enable_thorlabs_stage = True
                    self.last_thorlabs_angle = stage.degree
                    logger.info(f"Thorlabs K10CR2 {thorlabs_serial} connected")
                else:
                    logger.warning("Thorlabs K10CR2 connect() returned False")
            except Exception as e:
                logger.warning(f"failed to connect to Thorlabs K10CR2: {e}")

    def _lock(self) -> asyncio.Lock:
        """Return the SDK lock, building it lazily on the active loop."""
        if self._sdk_lock is None:
            self._sdk_lock = asyncio.Lock()
        return self._sdk_lock

    async def connect_hardware(self):
        """Connect to spectrometer."""
        if self.is_connected:
            return

        logger.info("connecting to spectrometer...")

        async with self._lock():
            if self.dm:
                try:
                    await self.dm.stop()
                except Exception:
                    pass
                self.dm = None

            self.dm = DeviceManager(start_icl=True)
            await self.dm.start()

            logger.info("Waiting for hardware discovery...")

            for _ in range(20):
                if self.dm.monochromators and self.dm.charge_coupled_devices:
                    break
                await asyncio.sleep(0.5)

            monos = self.dm.monochromators
            ccds = self.dm.charge_coupled_devices

            if not monos or not ccds:
                await self.dm.stop()
                raise RuntimeError(f"Hardware not found in time. (Monos: {len(monos)}, CCDs: {len(ccds)})")

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
            logger.success("spectrometer initialisation complete")

    async def acquire_spectrum(self, **kwargs) -> tuple[Any, Any]:
        if not self.is_connected:
            await self.connect_hardware()

        center_wavelength = kwargs.get("center_wavelength", 780)
        exposure         = kwargs.get("exposure", 1)
        grating          = kwargs.get("grating")
        slit_position    = kwargs.get("slit_position", 0.1)
        gain             = kwargs.get("gain", 0)
        speed            = kwargs.get("speed", 2)
        rotation_angle   = kwargs.get("rotation_angle", None)
        thorlabs_angle   = kwargs.get("thorlabs_angle", None)

        y_origin = kwargs.get("ccd_y_origin", 0)
        y_size   = kwargs.get("ccd_y_size", 256)
        x_bin    = kwargs.get("ccd_x_bin", 1)

        # ── Move stages (non-blocking on event loop) ─────────────────
        if rotation_angle is not None and self.enable_rotation_stage and self.rotation_stage:
            if abs(self.last_angle - rotation_angle) > 0.01:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: setattr(self.rotation_stage, 'degree', rotation_angle)
                )
                self.last_angle = rotation_angle
                logger.info(f"OptoSigma angle → {rotation_angle}°")

        if thorlabs_angle is not None and self.enable_thorlabs_stage and self.thorlabs_stage:
            if abs(self.last_thorlabs_angle - thorlabs_angle) > 0.001:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: setattr(self.thorlabs_stage, 'degree', thorlabs_angle)
                )
                self.last_thorlabs_angle = thorlabs_angle
                logger.info(f"Thorlabs angle → {thorlabs_angle}°")

        self._acquiring = True
        try:
            async with self._lock():
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
                    await self.mono.set_mirror_position(
                        self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL
                    )
                    await self._wait_for_mono(self.mono)
                    self._current_params['mirror'] = 'AXIAL'

                cfg    = await self.ccd.get_configuration()
                chip_x = int(cfg["chipWidth"])

                await self.ccd.set_acquisition_count(1)
                await self.ccd.set_center_wavelength(self.mono.id(), center_wavelength)
                await self.ccd.set_exposure_time(int(exposure * 1000))
                await self.ccd.set_gain(gain)
                await self.ccd.set_speed(speed)
                await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
                await self.ccd.set_acquisition_format(1, AcquisitionFormat.SPECTRA)
                await self.ccd.set_region_of_interest(
                    1, 0, int(y_origin), chip_x, int(y_size), int(x_bin), int(y_size)
                )
                await self.ccd.set_x_axis_conversion_type(
                    XAxisConversionType.FROM_ICL_SETTINGS_INI
                )

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

        except Exception:
            logger.exception("failed to acquire spectrum")
            # Mark the controller stale so the next call triggers a
            # fresh connect_hardware. Do NOT call dm.stop() here — that
            # tears down icl.exe and forces a 10 s reboot every time
            # there is a transient ICL hiccup. The reconnect path in
            # connect_hardware handles a stale device manager safely.
            self.is_connected = False
            raise
        finally:
            self._acquiring = False

    # ── OptoSigma rotation stage ──────────────────────────────────────

    async def set_rotation_angle(self, value: float) -> None:
        if self.enable_rotation_stage and self.rotation_stage and self.rotation_stage.is_connected:
            # Run the blocking serial move in a thread so the event loop
            # stays responsive for temperature polls and GUI updates.
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: setattr(self.rotation_stage, 'degree', value)
            )
            self.last_angle = value

    async def get_rotation_angle(self) -> float:
        if self.enable_rotation_stage and self.rotation_stage and self.rotation_stage.is_connected:
            self.last_angle = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.rotation_stage.degree
            )
            return self.last_angle
        return self.last_angle

    async def return_rotation_to_origin(self) -> None:
        if self.enable_rotation_stage and self.rotation_stage and self.rotation_stage.is_connected:
            await asyncio.get_event_loop().run_in_executor(
                None, self.rotation_stage.return_to_origin
            )
            self.last_angle = 0.0

    # ── Thorlabs rotation stage ───────────────────────────────────────

    async def set_thorlabs_angle(self, value: float) -> None:
        if self.enable_thorlabs_stage and self.thorlabs_stage and self.thorlabs_stage.is_connected:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: setattr(self.thorlabs_stage, 'degree', value)
            )
            self.last_thorlabs_angle = self.thorlabs_stage.degree

    async def get_thorlabs_angle(self) -> float:
        if self.enable_thorlabs_stage and self.thorlabs_stage and self.thorlabs_stage.is_connected:
            self.last_thorlabs_angle = self.thorlabs_stage.degree
            return self.last_thorlabs_angle
        return self.last_thorlabs_angle

    async def home_thorlabs_stage(self) -> None:
        if self.enable_thorlabs_stage and self.thorlabs_stage and self.thorlabs_stage.is_connected:
            await asyncio.get_event_loop().run_in_executor(
                None, self.thorlabs_stage.home
            )
            self.last_thorlabs_angle = 0.0

    # ── CCD temperature ───────────────────────────────────────────────

    async def get_ccd_temperature(self) -> float:
        # Skip if a scan is in progress — querying the CCD mid-acquisition
        # can cause command collisions over the SDK socket.
        if self._acquiring:
            return -999.0
        if self.is_connected and self.ccd:
            try:
                async with self._lock():
                    return await self.ccd.get_chip_temperature()
            except Exception as e:
                logger.warning(f"Failed to read temperature: {e}")
                return -999.0
        return 0.0

    # ── Acquisition abort ─────────────────────────────────────────────

    async def acquisition_abort(self) -> None:
        """Cancel any in-flight CCD acquisition and wait until idle.

        Safe to call when disconnected or while no acquisition is
        running; it returns immediately in that case.
        """
        if not (self.is_connected and self.ccd):
            return
        async with self._lock():
            await self.ccd.acquisition_abort()
            # Poll up to ~2 s for the busy flag to drop.
            for _ in range(40):
                if not await self.ccd.get_acquisition_busy():
                    return
                await asyncio.sleep(0.05)

    # ── Internal helpers ──────────────────────────────────────────────

    async def _wait_for_mono(self, mono: Monochromator) -> None:
        while await mono.is_busy():
            await asyncio.sleep(0.1)

    async def _wait_for_ccd(self, ccd: ChargeCoupledDevice) -> None:
        while await ccd.get_acquisition_busy():
            await asyncio.sleep(0.05)

    async def shutdown(self) -> None:
        logger.info("Shutting down hardware...")

        if self.enable_rotation_stage and self.rotation_stage:
            try:
                self.rotation_stage.disconnect()
            except Exception:
                pass

        if self.enable_thorlabs_stage and self.thorlabs_stage:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, self.thorlabs_stage.disconnect
                )
            except Exception:
                pass

        # Spectrometer
        if self.is_connected:
            try:
                async with self._lock():
                    if self.ccd:
                        await self.ccd.close()
                    if self.mono:
                        await self.mono.close()
                    if self.dm:
                        await self.dm.stop()
            except Exception as e:
                logger.error(f"error closing devices: {e}")
            self.is_connected = False

        logger.success("shutdown complete")