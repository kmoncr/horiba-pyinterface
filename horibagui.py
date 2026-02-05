import os
import sys
import asyncio
import threading
from time import sleep
from loguru import logger

from pymeasure.display.Qt import QtWidgets
from PyQt5.QtWidgets import (
    QLabel, QHBoxLayout, QGroupBox, QComboBox, 
    QPushButton, QVBoxLayout, QDoubleSpinBox, QFormLayout,
    QWidget, QFrame, QMessageBox
)
from PyQt5.QtCore import pyqtSignal, QTimer
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure, GRATING_CHOICES
from pymeasure.experiment import Results

try:
    from horibacontroller import HoribaController
except ImportError:
    logger.critical("failed to import horibacontroller")
    sys.exit(1)

class CollapsibleSection(QWidget):
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
    
    def set_content(self, widget):
        self._content_widget = widget
        widget.setParent(self._content_container)
        self._content_layout.addWidget(widget)
        widget.setVisible(True)
        self._content_container.setVisible(not self._is_collapsed)


class MainWindow(ManagedWindow):
    
    temp_updated_signal = pyqtSignal(float)
    angle_updated_signal = pyqtSignal(float)
    
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
        self.setWindowTitle('Horiba Spectrum Scan')
        self.setMinimumSize(1200, 800)

        self.temp_updated_signal.connect(self.on_temp_ui_update)
        self.angle_updated_signal.connect(self.on_angle_ui_update)

        self.loop = None
        self.loop_thread = None
        self._start_event_loop()
        
        self.controller = HoribaController(enable_logging=True)
        
        try:
            self.run_async_task(self.controller.connect_hardware())
        except Exception as e:
            logger.error(f"Hardware connection failed: {e}")
            QMessageBox.critical(self, "Connection Error", f"Failed to connect to hardware:\n{e}")
        
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
        
        self.setup_tools_ui() 
        self.inputs.layout().addWidget(self.tools_group)

        self._make_sequencer_collapsible()
        self.file_input.extensions = ['csv']
        
        self.update_current_angle()
        
        self.temp_timer = QTimer()
        self.temp_timer.timeout.connect(self.trigger_temperature_update)
        self.temp_timer.start(5000) 

    def setup_tools_ui(self):
        self.tools_group = QGroupBox("Tools")
        tools_layout = QVBoxLayout()
        
        self.temp_label = QLabel("CCD Temp: -- °C")
        self.temp_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #333;")
        tools_layout.addWidget(self.temp_label)
        
        btn_layout = QHBoxLayout()
        self.btn_rtc = QPushButton("RTC")
        self.btn_rtc.clicked.connect(lambda: self.launch_external_tool("rtc.py"))
        self.btn_image = QPushButton("Image Scan")
        self.btn_image.clicked.connect(lambda: self.launch_external_tool("image.py"))
        
        btn_layout.addWidget(self.btn_rtc)
        btn_layout.addWidget(self.btn_image)
        tools_layout.addLayout(btn_layout)
        self.tools_group.setLayout(tools_layout)

    def trigger_temperature_update(self):
        if not self.controller or not self.controller.is_connected:
            self.temp_label.setText("CCD Temp: Disconnected")
            return

        future = asyncio.run_coroutine_threadsafe(
            self.controller.get_ccd_temperature(), 
            self.loop 
        )
        future.add_done_callback(self._handle_temp_result)

    def _handle_temp_result(self, fut):
        try:
            temp = fut.result()
            self.temp_updated_signal.emit(temp)
        except Exception:
            self.temp_updated_signal.emit(-999.0)

    def on_temp_ui_update(self, temp):
        if temp == -999.0:
            self.temp_label.setText("CCD Temp: Err")
        else:
            color = "green" if temp < -50 else "red" 
            self.temp_label.setText(f"CCD Temp: <font color='{color}'>{temp:.1f} °C</font>")

    def update_current_angle(self):
        logger.debug("Requesting current angle update...")
        future = asyncio.run_coroutine_threadsafe(
            self.controller.get_rotation_angle(),
            self.loop
        )
        future.add_done_callback(self._handle_angle_result)

    def _handle_angle_result(self, fut):
        try:
            angle = fut.result()
            self.angle_updated_signal.emit(angle)
        except Exception as e:
            logger.error(f"Angle fetch error: {e}")

    def on_angle_ui_update(self, angle):
        self.current_angle_display.setText(f"Current Angle: {angle:.2f}°")
        self.set_angle_input.setValue(angle)
        logger.info(f"GUI updated with angle: {angle:.2f}°")

    def launch_external_tool(self, script_name):
        if hasattr(self, 'timer') and self.timer.isActive():
            logger.info("pausing UI timer...")
            self.timer.stop()

        async def run_tool_sequence():
            logger.info("starting external tool")
            
            if self.controller.is_connected:
                logger.info("shutting down connection...")
                await self.controller.shutdown()
                self.controller.is_connected = False
                await asyncio.sleep(2.0)
            
            logger.info(f"launching {script_name}...")
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, script_name,
                    stdout=None, 
                    stderr=None
                )
                await process.wait()
                
                logger.info(f"{script_name} finished. Waiting 2s before reconnect...")
                await asyncio.sleep(2.0) 
                
            except Exception as e:
                logger.error(f"failed to run tool: {e}")

            logger.info("reconnecting hardware to GUI...")
            try:
                await self.controller.connect_hardware()
                
                if self.controller.rotation_stage:
                    self.controller.last_angle = self.controller.rotation_stage.degree
                
                QTimer.singleShot(0, self.on_tool_sequence_finished)
                
            except Exception as e:
                logger.error(f"Failed to reconnect hardware: {e}")

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(run_tool_sequence(), self.loop)
        else:
            logger.error("Asyncio loop is not running!")

    def on_tool_sequence_finished(self):
        logger.info("Restoring UI state...")
        
        self.angle_updated_signal.emit(self.controller.last_angle)
        
        if hasattr(self, 'timer') and not self.timer.isActive():
            logger.info("Resuming UI polling timer.")
            self.timer.start()

    def _make_sequencer_collapsible(self):
        from pymeasure.display.widgets import SequencerWidget
        from PyQt5.QtWidgets import QDockWidget
        
        sequencer_widgets = self.findChildren(SequencerWidget)
        if not sequencer_widgets: return
        
        sequencer_widget = sequencer_widgets[0]
        parent = sequencer_widget.parent()
        
        if isinstance(parent, QDockWidget):
            self._sequencer_collapsible = CollapsibleSection("Sequencer (Angle Sweep)", start_collapsed=True)
            self._sequencer_collapsible.set_content(sequencer_widget)
            parent.setWidget(self._sequencer_collapsible)
            return
        
        if parent is not None:
            parent_layout = parent.layout()
            if parent_layout is not None:
                idx = parent_layout.indexOf(sequencer_widget)
                if idx >= 0:
                    parent_layout.removeWidget(sequencer_widget)
                    self._sequencer_collapsible = CollapsibleSection("Sequencer (Angle Sweep)", start_collapsed=True)
                    self._sequencer_collapsible.set_content(sequencer_widget)
                    if hasattr(parent_layout, 'insertWidget'):
                        parent_layout.insertWidget(idx, self._sequencer_collapsible)
                    else:
                        parent_layout.addWidget(self._sequencer_collapsible)

    def _start_event_loop(self):
        def run_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()
        
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=run_loop, args=(self.loop,), daemon=True)
        self.loop_thread.start()

    def run_async_task(self, task, timeout=30):
        try:
            future = asyncio.run_coroutine_threadsafe(task, self.loop)
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Error running async task: {e}")
            raise

    def do_go_to_angle(self):
        target_angle = self.set_angle_input.value()
        logger.info(f"GUI: Setting angle to {target_angle}°")
        
        async def _set_and_update():
            await self.controller.set_rotation_angle(target_angle)
            return await self.controller.get_rotation_angle()

        future = asyncio.run_coroutine_threadsafe(_set_and_update(), self.loop)
        future.add_done_callback(self._handle_angle_result)

    def do_return_to_origin(self):
        logger.info("GUI: Returning to origin")
        
        async def _home_and_update():
            await self.controller.return_rotation_to_origin()
            return await self.controller.get_rotation_angle()

        future = asyncio.run_coroutine_threadsafe(_home_and_update(), self.loop)
        future.add_done_callback(self._handle_angle_result)

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
        
        if rotation_angle is not None:
            procedure.rotation_angle = rotation_angle
        else:
            procedure.rotation_angle = self.set_angle_input.value()

        procedure.grating = self.grating_combo.currentText()
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
        sleep(0.5)

    def unique_filename(self, directory, base_filename, rotation_angle, scan_number):
        counter = 1
        angle_str = f"{rotation_angle:.1f}deg"
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