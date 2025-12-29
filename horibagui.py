import os
import sys
import asyncio
import threading
from loguru import logger
from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import (
    QLabel, QHBoxLayout, QGroupBox, QComboBox, 
    QPushButton, QVBoxLayout, QDoubleSpinBox, QFormLayout,
    QWidget, QSizePolicy, QFrame
)
from PyQt5.QtCore import Qt, pyqtSignal
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure, GRATING_CHOICES
from pymeasure.experiment import Results
from time import sleep

NO_HARDWARE = '--no-hardware' in sys.argv or '--debug' in sys.argv
if NO_HARDWARE:
    sys.argv = [arg for arg in sys.argv if arg not in ('--no-hardware', '--debug')]
    logger.warning("no hardware mode")

if not NO_HARDWARE:
    from horibacontroller import HoribaController

class CollapsibleSection(QWidget):
    """collapsible section widget for sequencer"""
    def __init__(self, title="", parent=None, start_collapsed=False):
        super().__init__(parent)
        
        self._is_collapsed = start_collapsed
        self._title = title
        self._content_widget = None
        
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        
        self._header = QPushButton()
        self._header.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 8px;
                font-weight: bold;
                background-color: #4a86c7;
                color: white;
                border: none;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #3a76b7;
            }
        """)
        self._header.clicked.connect(self.toggle)
        self._update_header()
        
        self._layout.addWidget(self._header)
        
        self._content_container = QFrame()
        self._content_container.setFrameShape(QFrame.StyledPanel)
        self._content_layout = QVBoxLayout(self._content_container)
        self._content_layout.setContentsMargins(5, 5, 5, 5)
        self._layout.addWidget(self._content_container)
        
        self._content_container.setVisible(not start_collapsed)
    
    def _update_header(self):
        arrow = "▼" if not self._is_collapsed else "▶"
        self._header.setText(f"{arrow}  {self._title}")
    
    def toggle(self):
        self._is_collapsed = not self._is_collapsed
        self._content_container.setVisible(not self._is_collapsed)
        self._update_header()
        logger.info(f"Sequencer collapsed: {self._is_collapsed}")
    
    def set_content(self, widget):
        """Set the widget to show/hide."""
        self._content_widget = widget
        widget.setParent(self._content_container)
        self._content_layout.addWidget(widget)
        widget.setVisible(True)
        self._content_container.setVisible(not self._is_collapsed)


class MockController:
    """Mock controller for GUI testing without hardware."""
    def __init__(self):
        self.last_angle = 0.0
        self.is_connected = False
        logger.info("MockController initialized (no hardware)")
    
    async def connect_hardware(self):
        logger.info("MockController: Simulating hardware connection...")
        await asyncio.sleep(0.5)
        self.is_connected = True
        logger.success("MockController: Fake hardware connected")
    
    async def acquire_spectrum(self, **kwargs):
        import numpy as np
        logger.info(f"MockController: Simulating spectrum acquisition with params: {kwargs}")
        await asyncio.sleep(kwargs.get('exposure', 1.0))
        # fake spectrum
        x = np.linspace(500, 600, 1024).tolist()
        y = (np.random.rand(1024) * 1000 + 500).tolist()
        return x, y
    
    async def set_rotation_angle(self, value: float):
        logger.info(f"MockController: Setting angle to {value}°")
        await asyncio.sleep(0.2)
        self.last_angle = value
    
    async def get_rotation_angle(self) -> float:
        return self.last_angle
    
    async def return_rotation_to_origin(self):
        logger.info("MockController: Returning to origin")
        await asyncio.sleep(0.2)
        self.last_angle = 0.0
    
    async def shutdown(self):
        logger.info("MockController: Shutting down")
        self.is_connected = False


class MainWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed',
                'ccd_y_origin', 'ccd_y_size', 'ccd_x_bin'
            ],
            displays=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed',
                'ccd_y_origin', 'ccd_y_size', 'ccd_x_bin'
            ],
            x_axis='Wavelength',
            y_axis='Intensity',
            sequencer=True,
            sequencer_inputs=['rotation_angle'],
        )
        self.setWindowTitle('Horiba Spectrum Scan' + (' [NO HARDWARE]' if NO_HARDWARE else ''))
        
        self.setMinimumSize(1200, 800)

        self.loop = None
        self.loop_thread = None
        self._start_event_loop()
        
        if NO_HARDWARE:
            self.controller = MockController()
        else:
            self.controller = HoribaController(enable_logging=True)
        
        self.run_async_task(self.controller.connect_hardware())
        
        grating_widget = QGroupBox("Grating Control")
        grating_layout = QHBoxLayout()
        self.grating_combo = QComboBox()
        self.grating_combo.addItems(GRATING_CHOICES.keys())
        self.grating_combo.setCurrentText('Third (150 grooves/mm)')
        self.grating_combo.currentTextChanged.connect(self.update_grating)
        grating_layout.addWidget(QLabel("Current Grating:"))
        grating_layout.addWidget(self.grating_combo)
        grating_widget.setLayout(grating_layout)

        scan_count_widget = QGroupBox("Scan Sequence Control")
        scan_count_layout = QFormLayout()
        self.scans_per_angle_input = QtWidgets.QSpinBox()
        self.scans_per_angle_input.setRange(1, 100)
        self.scans_per_angle_input.setValue(1) 
        scan_count_layout.addRow("Scans per Angle:", self.scans_per_angle_input)
        scan_count_widget.setLayout(scan_count_layout)

        rotation_widget = QGroupBox("Rotation Stage Control")
        rotation_layout = QVBoxLayout()
        
        current_angle_layout = QHBoxLayout()
        self.current_angle_display = QLabel("Current Angle: --.-°")
        self.refresh_angle_button = QPushButton("Refresh")
        self.refresh_angle_button.clicked.connect(self.update_current_angle)
        current_angle_layout.addWidget(self.current_angle_display)
        current_angle_layout.addWidget(self.refresh_angle_button)
        rotation_layout.addLayout(current_angle_layout)

        set_angle_layout = QFormLayout()
        self.set_angle_input = QDoubleSpinBox()
        self.set_angle_input.setRange(-360.0, 360.0)
        self.set_angle_input.setDecimals(2)
        self.set_angle_input.setValue(self.controller.last_angle)
        self.go_to_angle_button = QPushButton("Go to Angle")
        self.go_to_angle_button.clicked.connect(self.do_go_to_angle)
        set_angle_layout.addRow("Set Angle (deg):", self.set_angle_input)
        set_angle_layout.addRow(self.go_to_angle_button)
        rotation_layout.addLayout(set_angle_layout)
        
        self.return_to_origin_button = QPushButton("Return to Origin (0°)")
        self.return_to_origin_button.clicked.connect(self.do_return_to_origin)
        rotation_layout.addWidget(self.return_to_origin_button)

        rotation_widget.setLayout(rotation_layout)
        
        self.inputs.layout().addWidget(grating_widget)
        self.inputs.layout().addWidget(scan_count_widget)
        self.inputs.layout().addWidget(rotation_widget)
        self._make_sequencer_collapsible()

        self.file_input.extensions = ['csv']
        
        self.update_current_angle()

    def _make_sequencer_collapsible(self):
        """Make the sequencer widget collapsible and start it collapsed."""
        from pymeasure.display.widgets import SequencerWidget
        from PyQt5.QtWidgets import QDockWidget
        
        sequencer_widgets = self.findChildren(SequencerWidget)
        
        if not sequencer_widgets:
            logger.warning("Could not find sequencer widget")
            return
        
        sequencer_widget = sequencer_widgets[0]
        parent = sequencer_widget.parent()
        
        if isinstance(parent, QDockWidget):
            logger.info("Sequencer is in a QDockWidget, using setWidget()")
            
            self._sequencer_collapsible = CollapsibleSection(
                "Sequencer (Angle Sweep)", 
                start_collapsed=True
            )
            self._sequencer_collapsible.set_content(sequencer_widget)
            
            parent.setWidget(self._sequencer_collapsible)
            
            logger.success("Sequencer wrapped in collapsible section")
            return
        
        if parent is not None:
            parent_layout = parent.layout()
            if parent_layout is not None:
                idx = parent_layout.indexOf(sequencer_widget)
                if idx >= 0:
                    parent_layout.removeWidget(sequencer_widget)
                    
                    self._sequencer_collapsible = CollapsibleSection(
                        "Sequencer (Angle Sweep)", 
                        start_collapsed=True
                    )
                    self._sequencer_collapsible.set_content(sequencer_widget)
                    
                    if hasattr(parent_layout, 'insertWidget'):
                        parent_layout.insertWidget(idx, self._sequencer_collapsible)
                    else:
                        parent_layout.addWidget(self._sequencer_collapsible)
                    
                    logger.success("Sequencer wrapped in collapsible section")
                    return
        
        logger.warning("Could not wrap sequencer in collapsible section")

    def _start_event_loop(self):
        def run_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
        
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=run_loop, args=(self.loop,), daemon=True)
        self.loop_thread.start()
        logger.info("Event loop started in background thread")

    def run_async_task(self, task, timeout=30):
        try:
            future = asyncio.run_coroutine_threadsafe(task, self.loop)
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Error running async task: {e}")
            raise

    def update_current_angle(self):
        logger.debug("Requesting current angle update...")
        async def _get_angle():
            angle = await self.controller.get_rotation_angle()
            self.current_angle_display.setText(f"Current Angle: {angle:.2f}°")
            self.set_angle_input.setValue(angle)
            logger.info(f"GUI updated with angle: {angle:.2f}°")
        
        self.run_async_task(_get_angle())

    def do_go_to_angle(self):
        target_angle = self.set_angle_input.value()
        logger.info(f"GUI: Setting angle to {target_angle}°")
        self.run_async_task(self.controller.set_rotation_angle(target_angle))
        self.current_angle_display.setText(f"Current Angle: {target_angle:.2f}°")


    def do_return_to_origin(self):
        logger.info("GUI: Returning to origin")
        self.run_async_task(self.controller.return_rotation_to_origin())
        self.current_angle_display.setText("Current Angle: 0.00°")
        self.set_angle_input.setValue(0.0)

    def update_grating(self, text):
        logger.info(f"GUI: Grating changed to {text}")

    def make_procedure(self, rotation_angle=None):
        procedure = self.procedure_class()
        procedure.controller = self.controller
        procedure.loop = self.loop

        param_names = [
            "excitation_wavelength", "center_wavelength", "exposure",
            "slit_position", "gain", "speed",
            "ccd_y_origin", "ccd_y_size", "ccd_x_bin"
        ]
        
        for param_name in param_names:
            if hasattr(self.inputs, param_name):
                value = getattr(self.inputs, param_name).value()
                setattr(procedure, param_name, value)
                logger.info(f"  {param_name}: {value}")
        
        if rotation_angle is not None:
            procedure.rotation_angle = rotation_angle
            logger.info(f"  rotation_angle (sequenced): {rotation_angle}")
        else:
            procedure.rotation_angle = self.set_angle_input.value()
            logger.info(f"  rotation_angle: {procedure.rotation_angle}")

        procedure.grating = self.grating_combo.currentText()
        logger.info(f"  grating: {procedure.grating}")
        
        return procedure

    def queue(self, procedure=None):
        if procedure is None:
            procedure = self.make_procedure() 
        
        scans_per_angle = self.scans_per_angle_input.value()
        base_rotation_angle = procedure.rotation_angle

        for i in range(1, scans_per_angle + 1):
            
            current_procedure = self.make_procedure(rotation_angle=base_rotation_angle)
            current_procedure.scan_number = i 
            
            filename = self.unique_filename(
                self.file_input.directory, 
                self.file_input.filename, 
                current_procedure.rotation_angle, 
                i
            )
            current_procedure.data_filename = filename
            
            experiment = self.new_experiment(Results(current_procedure, filename))
            self.manager.queue(experiment)
        
        self.update_current_angle()
        
        sleep(2)

    def unique_filename(self, directory, base_filename, rotation_angle, scan_number):
        counter = 1
        angle_str = f"{rotation_angle:.1f}deg"
        
        # Filename format: [base_filename]_[angle.Xdeg]_S[scan_number]_[counter].csv
        filename = f"{base_filename}_{angle_str}_S{scan_number}_{counter}.csv"
        file_path = os.path.join(directory, filename)

        while os.path.exists(file_path):
            counter += 1
            filename = f"{base_filename}_{angle_str}_S{scan_number}_{counter}.csv"
            file_path = os.path.join(directory, filename)
        
        logger.info(f"Generated unique filename: {file_path}")
        return file_path

    def closeEvent(self, event):
        logger.info("Closing application...")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.controller.shutdown(), 
                self.loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
        finally:
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)
                if self.loop_thread:
                    self.loop_thread.join(timeout=2)
        
        event.accept()

if __name__ == "__main__":
    app = QtWidgets.QApplication([])
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
