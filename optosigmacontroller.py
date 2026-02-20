from loguru import logger
from optosigma import GSC01

class OptoSigmaController:
    def __init__(self, port: str = "COM3", timeout: int = 1):
        self.port = port
        self.timeout = timeout
        self.controller = None
        self._is_connected = False
        self._current_position = 0  
        
        # OSMS-60YAW specifications
        self.degree_per_pulse = 0.0025  # [deg/pulse] for OSMS-60YAW
        self.max_degree = 360.0
        
    def connect(self):
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
        if self.controller is not None:
            try:
                self.controller.close()
                logger.info("disconnected from OptoSigma rotation stage")
            except Exception as e:
                logger.error(f"error disconnecting: {str(e)}")
        self._is_connected = False
        self.controller = None
    
    def _update_current_position(self):
        if self._is_connected and self.controller:
            try:
                self._current_position = self.controller.position
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
            self._update_current_position()
            if self._current_position is None:
                logger.error("Position read returned None")
                return 0.0
            deg = (self._current_position % (self.max_degree / self.degree_per_pulse)) * self.degree_per_pulse
            logger.debug(f"Read position: {self._current_position} pulses = {deg:.2f}°")
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
            
            self.controller.position = target_position
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
            self.controller.return_origin()
            self.controller.sleep_until_stop()
            self._current_position = 0
            logger.info("rotation stage returned to origin")
        except Exception as e:
            logger.error(f"failed to return to origin: {str(e)}")
    
    def stop(self):
        if self.is_connected:
            try:
                self.controller.stop()
                logger.info("rotation stage stopped")
            except Exception as e:
                logger.error(f"failed to stop stage: {str(e)}")
    
    @property
    def is_busy(self) -> bool:
        if not self.is_connected:
            return False
        try:
            return not self.controller.is_ready
        except Exception as e:
            logger.error(f"failed to check busy status: {str(e)}")
            return False
    
    def wait_until_ready(self):
        if self.is_connected:
            try:
                self.controller.sleep_until_stop()
            except Exception as e:
                logger.error(f"error waiting for stage: {str(e)}")
    
    def get_status(self) -> dict:
        if not self.is_connected:
            return {"connected": False}
        
        try:
            return {
                "connected": True,
                "position_pulses": self._current_position,
                "degree": self.degree,
                "is_busy": self.is_busy,
                "is_ready": self.controller.is_ready if self.controller else False
            }
        except Exception as e:
            logger.error(f"failed to get status: {str(e)}")
            return {"connected": True, "error": str(e)}
