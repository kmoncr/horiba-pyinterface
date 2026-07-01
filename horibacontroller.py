import asyncio
import functools
from typing import Any
import numpy as np
from loguru import logger

# The horiba_sdk websocket uses the default ``websockets.connect``
# settings, which cap incoming messages at 1 MiB and run a keepalive
# ping every 20 s. A full-chip image frame (1024×256 doubles) exceeds
# the size cap and the keepalive can also kick in mid-acquisition,
# closing the connection. Disable both limits before any horiba_sdk
# code imports it. This patch must run BEFORE the SDK imports below.
import websockets

websockets.connect = functools.partial(
    websockets.connect, max_size=None, ping_interval=None
)

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
            "wavelength": None,
            "grating": None,
            "slit": None,
            "mirror": None,
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
        """Connect to spectrometer (and any auxiliary stage that came
        loose since the last call)."""
        # Defensive stage reconnect happens regardless of whether the
        # spectrometer is already connected — a child window might have
        # left a stage in a half-state without touching is_connected on
        # the controller.
        if self.enable_rotation_stage and self.rotation_stage is not None:
            if not self.rotation_stage.is_connected:
                try:
                    if self.rotation_stage.reconnect():
                        # Refresh last_angle from the live stage, not
                        # the cached value which may be stale.
                        try:
                            self.last_angle = self.rotation_stage.degree
                        except Exception as e:
                            logger.warning(
                                f"could not read OptoSigma angle after reconnect: {e}"
                            )
                except AttributeError:
                    # Older OptoSigmaController without reconnect().
                    pass

        if self.enable_thorlabs_stage and self.thorlabs_stage is not None:
            if not self.thorlabs_stage.is_connected:
                try:
                    self.thorlabs_stage.connect()
                    try:
                        self.last_thorlabs_angle = self.thorlabs_stage.degree
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"could not reconnect Thorlabs stage: {e}")

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
                raise RuntimeError(
                    f"Hardware not found in time. (Monos: {len(monos)}, CCDs: {len(ccds)})"
                )

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

            # NOTE: a saved grating-zero calibration is deliberately NOT
            # reapplied here. Feeding a fragile post-home wavelength read into
            # mono_setPosition during the most delicate moment of startup
            # corrupted the wavelength frame and left basic acquisition broken
            # until a power cycle. Calibration reapply is now an explicit,
            # user-initiated action — see reapply_saved_calibration() and the
            # "Reapply saved" button in the Grating Calibration window.

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
        thorlabs_angle = kwargs.get("thorlabs_angle", None)

        y_origin = kwargs.get("ccd_y_origin", 0)
        y_size = kwargs.get("ccd_y_size", 256)
        x_bin = kwargs.get("ccd_x_bin", 1)

        # ── Move stages (non-blocking on event loop) ─────────────────
        if (
            rotation_angle is not None
            and self.enable_rotation_stage
            and self.rotation_stage
        ):
            if abs(self.last_angle - rotation_angle) > 0.01:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: setattr(self.rotation_stage, "degree", rotation_angle)
                )
                self.last_angle = rotation_angle
                logger.info(f"OptoSigma angle → {rotation_angle}°")

        if (
            thorlabs_angle is not None
            and self.enable_thorlabs_stage
            and self.thorlabs_stage
        ):
            if abs(self.last_thorlabs_angle - thorlabs_angle) > 0.001:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: setattr(self.thorlabs_stage, "degree", thorlabs_angle)
                )
                self.last_thorlabs_angle = thorlabs_angle
                logger.info(f"Thorlabs angle → {thorlabs_angle}°")

        self._acquiring = True
        try:
            async with self._lock():
                if self._current_params["grating"] != grating:
                    logger.debug(f"Setting grating to {grating}")
                    await self.mono.set_turret_grating(grating)
                    await self._wait_for_mono(self.mono)
                    self._current_params["grating"] = grating

                if self._current_params["wavelength"] != center_wavelength:
                    logger.debug(f"Moving to {center_wavelength} nm")
                    await self.mono.move_to_target_wavelength(center_wavelength)
                    await self._wait_for_mono(self.mono)
                    self._current_params["wavelength"] = center_wavelength

                if self._current_params["slit"] != slit_position:
                    logger.debug(f"Setting slit to {slit_position} mm")
                    await self.mono.set_slit_position(self.mono.Slit.A, slit_position)
                    await self._wait_for_mono(self.mono)
                    self._current_params["slit"] = slit_position

                if self._current_params["mirror"] != "AXIAL":
                    await self.mono.set_mirror_position(
                        self.mono.Mirror.ENTRANCE, self.mono.MirrorPosition.AXIAL
                    )
                    await self._wait_for_mono(self.mono)
                    self._current_params["mirror"] = "AXIAL"

                cfg = await self.ccd.get_configuration()
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
            # Clear param cache so all CCD/mono settings are re-applied
            # after reconnect — the freshly-opened hardware has defaults.
            self._current_params = {
                "wavelength": None,
                "grating": None,
                "slit": None,
                "mirror": None,
            }
            raise
        finally:
            self._acquiring = False

    async def acquire_image(self, **kwargs) -> np.ndarray:
        """Acquire a single 2D image frame from the CCD.

        Mirrors acquire_spectrum's setup but uses
        AcquisitionFormat.IMAGE and XAxisConversionType.NONE. The
        returned array is a fresh copy of the SDK payload, shape
        (y_size, x_size).

        Optional kwargs for ROI override: x_origin, y_origin, x_size,
        y_size, x_bin, y_bin. Defaults: full chip from get_configuration,
        x_bin=y_bin=1.

        Other kwargs honoured: exposure (s), gain, speed,
        center_wavelength, rotation_angle, thorlabs_angle.
        """
        if not self.is_connected:
            await self.connect_hardware()

        exposure = kwargs.get("exposure", 1.0)
        gain = kwargs.get("gain", 0)
        speed = kwargs.get("speed", 2)
        center_wl = kwargs.get("center_wavelength", None)
        rotation_angle = kwargs.get("rotation_angle", None)
        thorlabs_angle = kwargs.get("thorlabs_angle", None)

        # ── Move stages (outside the SDK lock) ──────────────────────
        if (
            rotation_angle is not None
            and self.enable_rotation_stage
            and self.rotation_stage
        ):
            if abs(self.last_angle - rotation_angle) > 0.01:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: setattr(self.rotation_stage, "degree", rotation_angle)
                )
                self.last_angle = rotation_angle

        if (
            thorlabs_angle is not None
            and self.enable_thorlabs_stage
            and self.thorlabs_stage
        ):
            if abs(self.last_thorlabs_angle - thorlabs_angle) > 0.001:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: setattr(self.thorlabs_stage, "degree", thorlabs_angle)
                )
                self.last_thorlabs_angle = thorlabs_angle

        self._acquiring = True
        try:
            async with self._lock():
                cfg = await self.ccd.get_configuration()
                chip_w = int(cfg["chipWidth"])
                chip_h = int(cfg["chipHeight"])

                x_origin = int(kwargs.get("x_origin", 0))
                y_origin = int(kwargs.get("y_origin", 0))
                x_size = int(kwargs.get("x_size", chip_w))
                y_size = int(kwargs.get("y_size", chip_h))
                x_bin = int(kwargs.get("x_bin", 1))
                y_bin = int(kwargs.get("y_bin", 1))

                await self.ccd.set_acquisition_count(1)
                if center_wl is not None:
                    await self.ccd.set_center_wavelength(self.mono.id(), center_wl)
                await self.ccd.set_exposure_time(int(exposure * 1000))
                await self.ccd.set_gain(gain)
                await self.ccd.set_speed(speed)
                await self.ccd.set_timer_resolution(TimerResolution.MILLISECONDS)
                await self.ccd.set_acquisition_format(1, AcquisitionFormat.IMAGE)
                await self.ccd.set_region_of_interest(
                    1, x_origin, y_origin, x_size, y_size, x_bin, y_bin
                )
                await self.ccd.set_x_axis_conversion_type(XAxisConversionType.NONE)

                if not await self.ccd.get_acquisition_ready():
                    raise RuntimeError("CCD not ready for image acquisition")

                await self.ccd.acquisition_start(open_shutter=True)
                if exposure > 0.1:
                    await asyncio.sleep(exposure * 0.9)
                await self._wait_for_ccd(self.ccd)

                raw = await self.ccd.get_acquisition_data()
                y_data = raw[0]["roi"][0]["yData"]

                # np.array(...) always copies — never share memory with
                # the SDK's payload list.
                arr = np.array(y_data, dtype=float)
                # Some SDK payloads come pre-shaped, others come flat.
                effective_y = max(1, y_size // max(1, y_bin))
                effective_x = max(1, x_size // max(1, x_bin))
                if arr.ndim == 1:
                    arr = arr.reshape(effective_y, effective_x)
                return arr

        except Exception:
            logger.exception("failed to acquire image")
            self.is_connected = False
            raise
        finally:
            self._acquiring = False

    # ── OptoSigma rotation stage ──────────────────────────────────────

    async def set_rotation_angle(self, value: float) -> None:
        if (
            self.enable_rotation_stage
            and self.rotation_stage
            and self.rotation_stage.is_connected
        ):
            # Run the blocking serial move in a thread so the event loop
            # stays responsive for temperature polls and GUI updates.
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: setattr(self.rotation_stage, "degree", value)
            )
            self.last_angle = value

    async def get_rotation_angle(self) -> float:
        if (
            self.enable_rotation_stage
            and self.rotation_stage
            and self.rotation_stage.is_connected
        ):
            return await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.rotation_stage.degree
            )
        return self.last_angle

    async def return_rotation_to_origin(self) -> None:
        if (
            self.enable_rotation_stage
            and self.rotation_stage
            and self.rotation_stage.is_connected
        ):
            await asyncio.get_event_loop().run_in_executor(
                None, self.rotation_stage.return_to_origin
            )
            self.last_angle = 0.0

    # ── Thorlabs rotation stage ───────────────────────────────────────

    async def set_thorlabs_angle(self, value: float) -> None:
        if (
            self.enable_thorlabs_stage
            and self.thorlabs_stage
            and self.thorlabs_stage.is_connected
        ):
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: setattr(self.thorlabs_stage, "degree", value)
            )
            self.last_thorlabs_angle = self.thorlabs_stage.degree

    async def get_thorlabs_angle(self) -> float:
        if (
            self.enable_thorlabs_stage
            and self.thorlabs_stage
            and self.thorlabs_stage.is_connected
        ):
            return self.thorlabs_stage.degree
        return self.last_thorlabs_angle

    async def home_thorlabs_stage(self) -> None:
        if (
            self.enable_thorlabs_stage
            and self.thorlabs_stage
            and self.thorlabs_stage.is_connected
        ):
            await asyncio.get_event_loop().run_in_executor(
                None, self.thorlabs_stage.home
            )
            self.last_thorlabs_angle = 0.0

    # ── Mono wavelength (read / move / calibrate) ─────────────────────

    async def get_current_wavelength(self) -> float:
        async with self._lock():
            return float(await self.mono.get_current_wavelength())

    async def move_to_wavelength(self, wavelength: float) -> None:
        async with self._lock():
            await self.mono.move_to_target_wavelength(wavelength)
            await self._wait_for_mono(self.mono)
            self._current_params["wavelength"] = wavelength

    async def calibrate_wavelength(self, wavelength: float) -> None:
        async with self._lock():
            await self.mono.calibrate_wavelength(wavelength)
            # Calibration shifts the reported-wavelength frame without
            # moving the grating. Invalidate the cached wavelength so
            # the next scan re-issues a move against the new frame.
            self._current_params["wavelength"] = None

    async def reapply_saved_calibration(self) -> float | None:
        """Shift the current wavelength frame by the persisted calibration
        offset, restoring a saved grating-zero calibration.

        This is the manual, user-initiated replacement for the old
        force-on-connect behaviour. Call it once after connecting/homing:
        it reads whatever wavelength the grating currently reports and
        applies ``mono_setPosition`` at that value plus the saved offset,
        so the correction is valid wherever the grating homed to. The read
        and the set happen atomically under the SDK lock.

        Returns the new reported wavelength, or ``None`` if nothing is saved.
        Applying more than once per home doubles the offset.
        """
        from grating_calib import load_saved_offset

        offset = load_saved_offset()
        if not offset:
            return None
        async with self._lock():
            p = float(await self.mono.get_current_wavelength())
            target = p + offset
            await self.mono.calibrate_wavelength(target)
            # Invalidate the cached wavelength so the next scan re-issues a
            # move against the shifted frame.
            self._current_params["wavelength"] = None
            logger.info(
                f"reapplied grating calibration offset {offset:+.3f} nm "
                f"({p:.3f} -> {target:.3f} nm)"
            )
            return target

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
        running; in those cases this is a no-op. The ICL rejects
        ``acquisition_abort`` with "CCD error: command failed" when
        nothing is acquiring, so we only fire the abort if the busy
        flag is actually set.
        """
        if not (self.is_connected and self.ccd):
            return
        async with self._lock():
            try:
                if not await self.ccd.get_acquisition_busy():
                    return
                await self.ccd.acquisition_abort()
                # Poll up to ~2 s for the busy flag to drop.
                for _ in range(40):
                    if not await self.ccd.get_acquisition_busy():
                        return
                    await asyncio.sleep(0.05)
            except Exception as e:
                # Don't propagate — the caller is typically closeEvent
                # and we don't want to hide a real shutdown failure
                # behind an abort that was racing the natural end of
                # an acquisition.
                logger.warning(f"acquisition_abort suppressed: {e}")

    # ── Internal helpers ──────────────────────────────────────────────

    async def _wait_for_mono(self, mono: Monochromator) -> None:
        while await mono.is_busy():
            await asyncio.sleep(0.1)

    async def _wait_for_ccd(self, ccd: ChargeCoupledDevice) -> None:
        while await ccd.get_acquisition_busy():
            await asyncio.sleep(0.05)

    async def shutdown_spectrometer(self) -> None:
        """Close the spectrometer (mono, CCD, DeviceManager) only.

        Leaves the rotation stages connected so the GUI keeps reading
        their live position. Used by tests of the spectrometer-only
        teardown path.
        """
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

    async def shutdown(self) -> None:
        """Full teardown — spectrometer and all auxiliary stages.

        Called from the main window's closeEvent only. Child windows
        share the controller and therefore must NOT call this on close.
        """
        logger.info("Shutting down hardware...")

        await self.shutdown_spectrometer()

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

        logger.success("shutdown complete")
