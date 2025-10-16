import os
import sys
import asyncio
from loguru import logger
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import (QLabel, QSpinBox, QDoubleSpinBox, QHBoxLayout, 
                            QPushButton, QGroupBox, QComboBox, QVBoxLayout)
from PyQt5.QtCore import QTimer
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure, GRATING_CHOICES
from horibacontroller import HoribaController
from pymeasure.experiment import Results
from time import sleep

class MainWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed', 'grating', 'rotation_angle'
            ],
            displays=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed', 'grating', 'rotation_angle'
            ],
            x_axis='Wavelength',
            y_axis='Intensity',
            sequencer=True,
            sequencer_inputs=['rotation_angle'],
        )
        self.setWindowTitle('Horiba Spectrum Scan')

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.controller = HoribaController(enable_logging=True)

        grating_widget = QGroupBox("Grating Control")
        grating_layout = QHBoxLayout()
        self.grating_combo = QComboBox()
        self.grating_combo.addItems(GRATING_CHOICES.keys())
        self.grating_combo.setCurrentText('Third (150 grooves/mm)')
        self.grating_combo.currentTextChanged.connect(self.update_grating)
        grating_layout.addWidget(QLabel("Current Grating:"))
        grating_layout.addWidget(self.grating_combo)
        grating_widget.setLayout(grating_layout)

        self.inputs.layout().addWidget(grating_widget)

        self.file_input.extensions = ['csv']

    def update_grating(self, text):
        logger.info(f"GUI: Grating changed to {text}")

    def make_procedure(self):
        procedure = self.procedure_class()
        procedure.controller = self.controller
        procedure.loop = self.loop

        for param_name in ["excitation_wavelength", "center_wavelength", "exposure",
                          "slit_position", "gain", "speed"]:
            if hasattr(self.inputs, param_name):
                value = getattr(self.inputs, param_name).value()
                setattr(procedure, param_name, value)
                logger.info(f"  {param_name}: {value}")
        
        procedure.grating = self.grating_combo.currentText()
        logger.info(f"  grating: {procedure.grating}")
        
        return procedure

    def queue(self, procedure=None):
        if procedure is None:
            procedure = self.make_procedure()
        
        filename = unique_filename(self.file_input.directory,
                                 self.file_input.filename)
        procedure.data_filename = filename
        
        experiment = self.new_experiment(Results(procedure, filename))
        self.manager.queue(experiment)
        sleep(2)  

    def closeEvent(self, event):
        """Clean shutdown"""
        if not self.loop.is_closed():
            try:
                self.loop.run_until_complete(self.controller.shutdown())
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
            finally:
                self.loop.close()
        event.accept()

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
