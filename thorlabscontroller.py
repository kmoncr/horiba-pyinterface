import time
import sys
import os
from loguru import logger


_kinesis_loaded = False


def _load_kinesis(kinesis_path: str = r"C:\Program Files\Thorlabs\Kinesis") -> bool:
    global _kinesis_loaded
    if _kinesis_loaded:
        return True

    try:
        import clr  # pythonnet
    except ImportError:
        logger.error("pythonnet not installed – run: pip install pythonnet")
        return False

    if not os.path.isdir(kinesis_path):
        logger.error(f"Kinesis folder not found: {kinesis_path}")
        return False

    sys.path.insert(0, kinesis_path)

    required_dlls = [
        "Thorlabs.MotionControl.DeviceManagerCLI",
        "Thorlabs.MotionControl.GenericMotorCLI",
        "Thorlabs.MotionControl.IntegratedStepperMotorsCLI",  # K10CR2
    ]
    for dll in required_dlls:
        try:
            clr.AddReference(dll)
        except Exception as e:
            logger.error(f"Could not load {dll}: {e}")
            return False

    _kinesis_loaded = True
    return True


def list_k10cr2_serials(
    kinesis_path: str = r"C:\Program Files\Thorlabs\Kinesis",
) -> list[str]:
    """Return the serial numbers of all connected Thorlabs K10CR2 stages.

    K10CR2 serials use the device prefix 55. We ask Kinesis for the device list
    filtered by CageRotator.DevicePrefix; if that overload isn't available we
    fall back to the full device list filtered by the "55" prefix.
    """
    if not _load_kinesis(kinesis_path):
        return []
    try:
        from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
        from Thorlabs.MotionControl.IntegratedStepperMotorsCLI import CageRotator

        DeviceManagerCLI.BuildDeviceList()
        try:
            serials = DeviceManagerCLI.GetDeviceList(CageRotator.DevicePrefix)
        except Exception:
            serials = [
                s for s in DeviceManagerCLI.GetDeviceList() if str(s).startswith("55")
            ]
        return [str(s) for s in serials]
    except Exception as e:
        logger.error(f"failed to list K10CR2 devices: {e}")
        return []


class ThorlabsK10CR2Controller:
    """
    Thin wrapper around the Kinesis IntegratedStepperMotors CLI for the
    K10CR2 rotation mount.
    """

    def __init__(
        self,
        serial_number: str,
        kinesis_path: str = r"C:\Program Files\Thorlabs\Kinesis",
        polling_ms: int = 250,
    ):
        self.serial_number = serial_number
        self.kinesis_path = kinesis_path
        self.polling_ms = polling_ms

        self._device = None
        self._is_connected = False
        self._last_degree = 0.0

    def connect(self) -> bool:
        if not _load_kinesis(self.kinesis_path):
            return False

        try:
            from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
            from Thorlabs.MotionControl.IntegratedStepperMotorsCLI import CageRotator
            from System import Decimal as CDecimal

            DeviceManagerCLI.BuildDeviceList()

            self._device = CageRotator.CreateCageRotator(self.serial_number)
            self._device.Connect(self.serial_number)

            if not self._device.IsSettingsInitialized():
                self._device.WaitForSettingsInitialized(5000)

            self._device.StartPolling(self.polling_ms)
            time.sleep(0.25)
            self._device.EnableDevice()
            time.sleep(0.25)

            motor_cfg = self._device.LoadMotorConfiguration(self.serial_number)
            motor_cfg.DeviceSettingsName = "K10CR2"
            motor_cfg.UpdateCurrentConfiguration()
            self._device.SetSettings(self._device.MotorDeviceSettings, True, False)

            self._is_connected = True
            logger.success(f"K10CR2 {self.serial_number} connected")

            try:
                self._last_degree = self._read_degree()
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"K10CR2 connect failed: {e}")
            self._is_connected = False
            return False

    def disconnect(self):
        if self._device is not None:
            try:
                self._device.StopPolling()
                self._device.Disconnect(True)
                logger.info(f"K10CR2 {self.serial_number} disconnected")
            except Exception as e:
                logger.error(f"K10CR2 disconnect error: {e}")
            finally:
                self._device = None
        self._is_connected = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._device is not None

    @property
    def degree(self) -> float:
        if not self.is_connected:
            return self._last_degree
        try:
            self._last_degree = self._read_degree()
            return self._last_degree
        except Exception as e:
            logger.error(f"K10CR2 read position failed: {e}")
            return self._last_degree

    @degree.setter
    def degree(self, target: float):
        if not self.is_connected:
            logger.error("K10CR2 not connected – cannot move")
            return
        target = float(target) % 360.0
        try:
            from System import Decimal as CDecimal

            self._device.MoveTo(CDecimal(target), 60000)
            self._last_degree = target
            logger.info(f"K10CR2 moved to {target:.3f}°")
        except Exception as e:
            logger.error(f"K10CR2 move failed: {e}")

    def home(self, timeout_ms: int = 60_000):
        if not self.is_connected:
            logger.error("K10CR2 not connected – cannot home")
            return
        try:
            self._device.Home(timeout_ms)
            self._last_degree = 0.0
            logger.info(f"K10CR2 {self.serial_number} homed")
        except Exception as e:
            logger.error(f"K10CR2 home failed: {e}")

    def return_to_origin(self):
        self.home()

    def move_relative(self, delta_degree: float):
        self.degree = self.degree + delta_degree

    def stop(self):
        if self.is_connected:
            try:
                self._device.Stop(0)
                logger.info("K10CR2 stopped")
            except Exception as e:
                logger.error(f"K10CR2 stop failed: {e}")

    @property
    def is_busy(self) -> bool:
        if not self.is_connected:
            return False
        try:
            return self._device.Status.IsInMotion
        except Exception:
            return False

    def wait_until_ready(self, poll_interval: float = 0.1, timeout: float = 65.0):
        """Block until the stage stops moving (or timeout elapses).

        MoveTo has its own 60 s timeout and can return before motion truly ends,
        so callers poll this afterward. The timeout here (slightly longer than
        MoveTo's) guards against a stuck IsInMotion flag hanging the caller.
        """
        deadline = time.monotonic() + timeout
        while self.is_busy:
            if time.monotonic() > deadline:
                logger.warning("K10CR2 wait_until_ready timed out; stage still busy")
                return
            time.sleep(poll_interval)

    def _read_degree(self) -> float:
        pos = self._device.Position
        return float(str(pos)) % 360.0
