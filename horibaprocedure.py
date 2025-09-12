from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter, ListParameter, Parameter
from enum import Enum

class GratingEnum(Enum):
    FIRST = "Monochromator.Grating.FIRST"
    SECOND = "Monochromator.Grating.SECOND"
    THIRD = "Monochromator.Grating.THIRD"
    
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
    def enumconv(self, param_name: str, value: str):
        # convert UI strings to SDK values / enums
        if param_name == 'grating':
            return GRATING_CHOICES.get(value).value if value in GRATING_CHOICES else None
        enum_dict = PARAM_MAP.get(param_name)
        if enum_dict and value in enum_dict:
            return enum_dict[value].value
        return None
        
    data_filename = Parameter("Filename")
    excitation_wavelength = FloatParameter("Excitation Wavelength", units='nm', default=532.0)
    center_wavelength = FloatParameter("Center Wavelength", units='nm', default=545.0)
    exposure = IntegerParameter("Exposure", units='s', default=1)
    grating = ListParameter("Grating", choices=list(GRATING_CHOICES.keys()), default='Third (150 grooves/mm)')
    slit_position = FloatParameter("Slit position", units='mm', default=0.1)
    gain = ListParameter("Gain", choices=list(GAIN_CHOICES.keys()), default='High Light')
    # speed previously had no default which caused "Missing 'speed'". Add explicit default.
    speed = ListParameter("Speed", choices=list(SPEED_CHOICES.keys()), default='50 kHz')

    DATA_COLUMNS = ["Wavenumber", "Intensity", "Wavelength"]

    def execute(self):
        import asyncio
        params = {
            'center_wavelength': self.center_wavelength,
            'exposure': self.exposure,
            'grating': self.enumconv('grating', self.grating),
            'slit_position': self.slit_position,
            'gain': self.enumconv('gain', self.gain),
            'speed': self.enumconv('speed', self.speed),
        }

        # controller is injected by the GUI (persistent per session)
        x_data, y_data = asyncio.run(self.controller.acquire_spectrum(**params))

        # flatten single-element nested lists from SDK
        if isinstance(x_data, list) and len(x_data) == 1:
            x_data = x_data[0]
        if isinstance(y_data, list) and len(y_data) == 1:
            y_data = y_data[0]

        for x, y in zip(x_data, y_data):
            # if x is pixel index, conversion to wavelength/wavenumber must be handled elsewhere
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
