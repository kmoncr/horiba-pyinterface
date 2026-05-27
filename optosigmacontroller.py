import threading
from loguru import logger
from optosigma import GSC01

class OptoSigmaController:
    def __init__(self, port: str = "COM3", timeout: int = 1):
        self.port = port
        self.timeout = timeout
        self.controller = None
        self._is_connected = False
        self._current_position = 0
        # Serializes every access to the GSC01 serial port. The vendor
        # library is a plain serial.Serial with no internal locking, so
        # without this the scan-path setter (run via executor in
        # horibacontroller.acquire_spectrum) can interleave reads with
        # a UI Refresh and corrupt readline() responses.
        self._lock = threading.RLock()

        # OSMS-60YAW specifications
        self.degree_per_pulse = 0.0025  # [deg/pulse] for OSMS-60YAW
        self.max_degree = 360.0

    def connect(self):
        with self._lock:
            try:
                self.controller = GSC01(self.port, timeout=self.timeout)
                self._is_connected = True
                logger.info(f"connected to OptoSigma stage on {self.port}")

                # Get current position
                self._update_current_position()
                return True

            except Exception as e:
                logger.error(f"failed to connect to OptoSigma stage: {str(e)}")
                self._is_connected = False
                return False

    def disconnect(self):
        with self._lock:
            if self.controller is not None:
                try:
                    self.controller.close()
                    logger.info("disconnected from OptoSigma rotation stage")
                except Exception as e:
                    logger.error(f"error disconnecting: {str(e)}")
            self._is_connected = False
            self.controller = None

    def reconnect(self) -> bool:
        """Bring the stage back up if it was disconnected.

        Returns True when the stage is connected (already-was or
        successfully reconnected), False on failure.
        """
        with self._lock:
            if self._is_connected and self.controller is not None:
                return True
        return self.connect()

    def _update_current_position(self):
        # Caller must hold self._lock.
        if self._is_connected and self.controller:
            try:
                pos = self.controller.position
                if pos is None:
                    logger.warning("Position read returned None; keeping cached value")
                    return
                # Reject reads that are implausibly far from the last known position.
                # More than 10° of deviation without a commanded move is a bad serial read.
                max_pulse_jump = int(10.0 / self.degree_per_pulse)  # 4000 pulses
                if abs(pos - self._current_position) > max_pulse_jump:
                    logger.warning(
                        f"Suspicious position read: {pos} pulses "
                        f"(cached {self._current_position}); keeping cached value"
                    )
                    return
                self._current_position = pos
            except Exception as e:
                logger.error(f"Failed to read position: {str(e)}")

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self.controller is not None

    @property
    def degree(self) -> float:
        if not self.is_connected:
            logger.warning("Attempted to read degree while disconnected - returning 0")
            return 0.0
        try:
            with self._lock:
                self._update_current_position()
                pos = self._current_position
            if pos is None:
                logger.error("Position read returned None")
                return 0.0
            deg = (pos % (self.max_degree / self.degree_per_pulse)) * self.degree_per_pulse
            logger.debug(f"Read position: {pos} pulses = {deg:.2f}°")
            return deg
        except Exception as e:
            logger.error(f"failed to get degree: {str(e)}")
            return 0.0

    @degree.setter
    def degree(self, target_degree: float):
        if not self.is_connected:
            logger.error("cannot set degree - stage not connected")
            return

        try:
            target_degree = target_degree % self.max_degree
            target_position = int(target_degree / self.degree_per_pulse)

            logger.debug(f"moving rotation stage to {target_degree:.2f} degrees ({target_position} pulses)")

            with self._lock:
                # Bypass the GSC01 library's position setter — it does a
                # read-then-relative-move which lands at the wrong absolute
                # position if the read is garbled. A: command is absolute.
                self.controller.set_absolute_pulse(target_position)
                self.controller.driving()
                self.controller.sleep_until_stop()
                self._current_position = target_position

            logger.info(f"rotation stage moved to {target_degree:.2f} degrees")

        except Exception as e:
            logger.error(f"failed to set degree: {str(e)}")

    def move_relative(self, delta_degree: float):
        current = self.degree
        target = current + delta_degree
        self.degree = target

    def return_to_origin(self):
        if not self.is_connected:
            logger.error("cannot return to origin - stage not connected")
            return

        try:
            logger.info("returning rotation stage to origin...")
            with self._lock:
                self.controller.return_origin()
                self.controller.sleep_until_stop()
                self._current_position = 0
            logger.info("rotation stage returned to origin")
        except Exception as e:
            logger.error(f"failed to return to origin: {str(e)}")

    def stop(self):
        if self.is_connected:
            try:
                with self._lock:
                    self.controller.stop()
                logger.info("rotation stage stopped")
            except Exception as e:
                logger.error(f"failed to stop stage: {str(e)}")

    @property
    def is_busy(self) -> bool:
        if not self.is_connected:
            return False
        try:
            with self._lock:
                return not self.controller.is_ready
        except Exception as e:
            logger.error(f"failed to check busy status: {str(e)}")
            return False

    def wait_until_ready(self):
        if self.is_connected:
            try:
                with self._lock:
                    self.controller.sleep_until_stop()
            except Exception as e:
                logger.error(f"error waiting for stage: {str(e)}")

    def get_status(self) -> dict:
        if not self.is_connected:
            return {"connected": False}

        try:
            with self._lock:
                is_ready = self.controller.is_ready if self.controller else False
                position_pulses = self._current_position
            return {
                "connected": True,
                "position_pulses": position_pulses,
                "degree": self.degree,
                "is_busy": not is_ready,
                "is_ready": is_ready,
            }
        except Exception as e:
            logger.error(f"failed to get status: {str(e)}")
            return {"connected": True, "error": str(e)}
