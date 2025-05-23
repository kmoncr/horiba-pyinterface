import asyncio
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter, ListParameter, Parameter
from horibacontroller import HoribaController
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
    def enumconv(self, param_name:str, value: str):
        if param_name == 'grating':
            if value in GRATING_CHOICES:
                return GRATING_CHOICES[value]
            else:
                print(f"Error: Grating choice '{value}' not found.")
                return None
        enum_dict = PARAM_MAP.get(param_name)

        if value in enum_dict:
            enum_value = enum_dict[value]
            return enum_value.value
        else: 
            print(f"Error: Value '{value}' not found in {param_name} choices.")
            return None
        
    data_filename = Parameter("Filename")
    excitation_wavelength = FloatParameter("Excitation Wavelength", units = 'nm', default = 532 )
    center_wavelength = FloatParameter("Center Wavelength", units = 'nm', default=545)
    exposure         = IntegerParameter("Exposure", units = 'ms', default=1000)
    grating = ListParameter("Grating", choices=list(GRATING_CHOICES.keys()), default='Third (150 grooves/mm)')
    # slit             = IntegerParameter("Slit", default = 1) //hardcoded slit, mirror selections
    slit_position    = FloatParameter("Slit position",  units = 'mm',        default=0.1)
    # mirror = IntegerParameter("Mirror", default = 1)
    #  mirror_position  = IntegerParameter("Mirror position",       default=0)
    gain = ListParameter("Gain", choices=['Ultimate Sensitivity', 'High Sensitivity', 'Best Dynamic Range', 'High Light'], default='High Light')
    speed = ListParameter("Speed", choices=['50 kHz', '1 mHz', '3 mHz'])#

    DATA_COLUMNS = ["Wavenumber", "Intensity", "Wavelength"]

    def execute(self):
        params = {
        'center_wavelength': self.center_wavelength,
        'exposure': self.exposure,
        'grating': self.enumconv('grating', self.grating),
        #'slit': self.slit,
        'slit_position': self.slit_position,
        #'mirror': self.mirror,
        # #'mirror_position': self.mirror_position,
        'gain': self.enumconv('gain', self.gain),
        'speed': self.enumconv('speed', self.speed),
    }
        
        self.controller = HoribaController()
        x_data, y_data = asyncio.run(self.controller.acquire_spectrum(**params))
        
        for i in range(len(y_data)):
            for x, y in zip(x_data, y_data[i]):
                self.emit("results", {
                    "Wavelength": x, 
                    "Wavenumber": (1/self.excitation_wavelength - 1 /x) * 1e7,
                    "Intensity": y
                })


    @property
    def procedure(self):
        return self
