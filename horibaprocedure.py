from pymeasure.experiment import (
    Procedure, FloatParameter, ListParameter, IntegerParameter
)
from enum import Enum
from horiba_sdk.devices.single_devices import Monochromator
from loguru import logger
import asyncio

class GratingEnum(Enum):
    FIRST = Monochromator.Grating.FIRST
    SECOND = Monochromator.Grating.SECOND
    THIRD = Monochromator.Grating.THIRD

GRATING_CHOICES = {
    'First (1800 grooves/mm)': GratingEnum.FIRST,
    'Second (600 grooves/mm)': GratingEnum.SECOND,
    'Third (150 grooves/mm)': GratingEnum.THIRD
}

class Gain(Enum):
    FIRST = 0
    SECOND = 1
    THIRD = 2
    FOURTH = 3

GAIN_CHOICES = {
    'Ultimate Sensitivity': Gain.THIRD,
    'High Sensitivity': Gain.SECOND, 
    'Best Dynamic Range': Gain.FOURTH,
    'High Light': Gain.FIRST
}

class Speed(Enum):
    FIRST = 0
    SECOND = 1
    THIRD = 2

SPEED_CHOICES = {
    '50 kHz': Speed.FIRST, 
    '1 MHz': Speed.SECOND, 
    '3 MHz': Speed.THIRD
}

PARAM_MAP = {
    'gain': GAIN_CHOICES,
    'speed': SPEED_CHOICES
}

class HoribaSpectrumProcedure(Procedure):
    excitation_wavelength = FloatParameter("Excitation Wavelength", units="nm", default=532.0)
    center_wavelength = FloatParameter("Center Wavelength", units="nm", default=545.0)
    exposure = FloatParameter("Exposure", units="s", default=1)
    slit_position = FloatParameter("Slit Position", units="mm", default=0.1)
    gain = ListParameter("Gain", choices=GAIN_CHOICES.keys(), default='Best Dynamic Range')
    speed = ListParameter("Speed", choices=SPEED_CHOICES.keys(), default='50 kHz')
    grating = ListParameter("Grating", choices=GRATING_CHOICES.keys(), default='Third (150 grooves/mm)')
    rotation_angle = FloatParameter("Rotation Angle", units="deg") 
    scan_number = IntegerParameter("Scan Number", default=1, minimum=1)
    
    DATA_COLUMNS = ["Wavenumber", "Intensity", "Wavelength", "Scan Number"]

    def __init__(self):
        super().__init__()
        self.controller = None
        self.loop = None

    def enumconv(self, param_name: str, value: str):
        """Convert GUI string values to SDK enum values"""
        logger.debug(f"Converting {param_name}: {value}")
        
        if param_name == 'grating':
            if value in GRATING_CHOICES:
                enum_val = GRATING_CHOICES[value]
                return enum_val.value
            logger.error(f"Invalid grating value: {value}")
            return None

        enum_dict = PARAM_MAP.get(param_name)
        if enum_dict and value in enum_dict:
            enum_val = enum_dict[value]
            logger.debug(f"Converted {param_name} {value} -> {enum_val.value}")
            return enum_val.value

        logger.error(f"Unknown parameter or value: {param_name}={value}")
        return None

    def run_async(self, coro):
        """Helper to run async coroutines from the worker thread"""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result()  # Block until complete

    def execute(self):
        """Execute a single measurement (one scan) at the current angle."""
        logger.info(f"Setting rotation angle to {self.rotation_angle}° for Scan {self.scan_number}")
        
        # Set the rotation angle once at the beginning of the procedure
        self.run_async(
            self.controller.set_rotation_angle(self.rotation_angle)
        )

        params = {
            'center_wavelength': self.center_wavelength,
            'exposure': self.exposure,
            'grating': self.enumconv('grating', self.grating),
            'slit_position': self.slit_position,
            'gain': self.enumconv('gain', self.gain),
            'speed': self.enumconv('speed', self.speed),
            'rotation_angle': self.rotation_angle
        }
        
        # Perform the single scan
        logger.info(f"Starting acquisition for Scan {self.scan_number} at angle {self.rotation_angle}°")
        
        x_data, y_data = self.run_async(
            self.controller.acquire_spectrum(**params)
        )
        
        if isinstance(x_data, list) and len(x_data) == 1:
            x_data = x_data[0]
        if isinstance(y_data, list) and len(y_data) == 1:
            y_data = y_data[0]
        
        for x, y in zip(x_data, y_data):
            try:
                wavenumber = (1.0 / self.excitation_wavelength - 1.0 / x) * 1e7
            except Exception as e:
                logger.error(f"Failed to calculate wavenumber: {e}")
                wavenumber = None
            
            self.emit('results', {
                "Wavenumber": wavenumber,
                "Intensity": y,
                "Wavelength": x,
                "Scan Number": self.scan_number
            })
        
        logger.success(f"Completed Scan {self.scan_number} at angle {self.rotation_angle}°")
        
        # Check for shutdown request after the scan
        if self.should_stop():
            logger.warning(f"Stop requested after scan {self.scan_number}. Stopping.")

    @property
    def procedure(self):
        return self