from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter, ListParameter, Parameter
from enum import Enum
from horiba_sdk.devices.single_devices import Monochromator
from loguru import logger  

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
    '1 mHz': Speed.SECOND, 
    '3 mHz': Speed.THIRD
}

PARAM_MAP = {
    'gain': GAIN_CHOICES,
    'speed': SPEED_CHOICES
}

class HoribaSpectrumProcedure(Procedure):
    
    data_filename = Parameter("Filename")
    excitation_wavelength = FloatParameter("Excitation Wavelength", units='nm', default=532.0)
    center_wavelength = FloatParameter("Center Wavelength", units='nm', default=545.0)
    exposure = IntegerParameter("Exposure", units='s', default=1)
    grating = ListParameter("Grating", choices=list(GRATING_CHOICES.keys()), default='Third (150 grooves/mm)')
    slit_position = FloatParameter("Slit position", units='mm', default=0.1)
    gain = ListParameter("Gain", choices=list(GAIN_CHOICES.keys()), default='High Light')
    speed = ListParameter("Speed", choices=list(SPEED_CHOICES.keys()), default='50 kHz')
    rotation_angle = FloatParameter("Rotation Angle", units="deg", default=0)

    DATA_COLUMNS = ["Wavenumber", "Intensity", "Wavelength"]
    
    def __init__(self):
        super().__init__()
        self.controller = None 
    
    def enumconv(self, param_name: str, value: str):
        """Convert GUI string values to SDK enum values"""
        logger.debug(f"Converting {param_name}: {value}")
        
        if param_name == 'grating':
            if value in GRATING_CHOICES:
                enum_val = GRATING_CHOICES[value]  
                logger.debug(f"Converted grating {value} -> {enum_val.value}")
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

    def execute(self):
        logger.debug(f"Raw grating parameter value: {self.grating}")
        params = {
            'center_wavelength': self.center_wavelength,
            'exposure': self.exposure,
            'grating': self.enumconv('grating', self.grating),
            'slit_position': self.slit_position,
            'gain': self.enumconv('gain', self.gain),
            'speed': self.enumconv('speed', self.speed),
        }
        logger.debug(f"Converted grating value: {params['grating']}")

        logger.info("Executing procedure with converted parameters:")
        for k, v in params.items():
            logger.info(f"  {k}: {v}")

        try:
            x_data, y_data = self.loop.run_until_complete(
                self.controller.acquire_spectrum(**params)
            )
        except Exception as e:
            logger.exception("acquire_spectrum failed")
            raise
        
        if isinstance(x_data, list) and len(x_data) == 1:
            x_data = x_data[0]
        if isinstance(y_data, list) and len(y_data) == 1:
            y_data = y_data[0]

        for x, y in zip(x_data, y_data):
            try:
                wavenumber = (1.0 / self.excitation_wavelength - 1.0 / x) * 1e7
            except Exception:
                wavenumber = None
            self.emit("results", {
                "Wavelength": x,
                "Wavenumber": wavenumber,
                "Intensity": y
            })

    @property
    def procedure(self):
        return self