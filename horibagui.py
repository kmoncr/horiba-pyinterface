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
    QWidget, QFrame, QMessageBox, QTabWidget, QGridLayout,
    QSizePolicy, QScrollArea, QSpinBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QRadioButton, QButtonGroup, QLineEdit, QSplitter,
    QDialog,
)
from PyQt5.QtCore import pyqtSignal, QTimer, Qt
from PyQt5.QtGui import QColor
from pymeasure.display.windows import ManagedWindow
from horibaprocedure import HoribaSpectrumProcedure, GRATING_CHOICES
from pymeasure.experiment import Results

try:
    from horibacontroller import HoribaController
except ImportError:
    logger.critical("failed to import horibacontroller")
    sys.exit(1)


# ── CollapsibleSection ────────────────────────────────────────────────────────

class CollapsibleSection(QWidget):
    def __init__(self, title="", parent=None, start_collapsed=False):
        super().__init__(parent)
        self._is_collapsed = start_collapsed
        self._title = title

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._header = QPushButton()
        self._header.setStyleSheet("""
            QPushButton {
                text-align: left; padding: 8px; font-weight: bold;
                background-color: #4a86c7; color: white;
                border: none; border-radius: 3px;
            }
            QPushButton:hover { background-color: #3a76b7; }
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
        widget.setParent(self._content_container)
        self._content_layout.addWidget(widget)
        widget.setVisible(True)
        self._content_container.setVisible(not self._is_collapsed)


# ── PopupSequencerButton ──────────────────────────────────────────────────────

class PopupSequencerButton(QWidget):
    """
    A header button that sits at the bottom of the panel in the normal flow.
    When clicked it opens a frameless QDialog anchored above the button.
    """
    def __init__(self, content_widget: QWidget, title: str = "Dual-Stage Synchronized Sequence", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._btn = QPushButton(f"▲  {title}")
        self._btn.setStyleSheet("""
            QPushButton {
                text-align: left; padding: 8px; font-weight: bold;
                background-color: #4a86c7; color: white;
                border: none; border-radius: 3px;
            }
            QPushButton:hover { background-color: #3a76b7; }
        """)
        self._btn.clicked.connect(self._toggle_popup)
        layout.addWidget(self._btn)

        self._content_widget = content_widget
        self._dialog = None
        self._open = False

    def _build_dialog(self):
        # Use Qt.Dialog | Qt.FramelessWindowHint instead of
        # Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint.
        # Qt.Tool windows are passive and don't receive input focus properly
        # on many platforms, causing clicks to be ignored.
        dlg = QDialog(self.window(), Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setStyleSheet("QDialog { border: 2px solid #4a86c7; background: white; }")

        # Ensure the dialog accepts focus and input
        dlg.setFocusPolicy(Qt.StrongFocus)
        dlg.setAttribute(Qt.WA_ShowWithoutActivating, False)

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(4, 4, 4, 4)
        self._content_widget.setParent(dlg)
        dlg_layout.addWidget(self._content_widget)
        dlg.setFixedWidth(max(self._btn.width(), 460))
        dlg.adjustSize()
        return dlg

    def _toggle_popup(self):
        if self._open and self._dialog and self._dialog.isVisible():
            self._dialog.hide()
            self._btn.setText(self._btn.text().replace("▼", "▲"))
            self._open = False
        else:
            if self._dialog is None:
                self._dialog = self._build_dialog()
            self._reposition()
            self._dialog.show()
            self._dialog.raise_()
            self._dialog.activateWindow()   # ensure the dialog gets input focus
            self._btn.setText(self._btn.text().replace("▲", "▼"))
            self._open = True

    def _reposition(self):
        if self._dialog is None:
            return
        btn_global = self._btn.mapToGlobal(self._btn.rect().topLeft())
        self._dialog.setFixedWidth(max(self._btn.width(), 460))
        self._dialog.adjustSize()
        popup_h = self._dialog.sizeHint().height()
        if popup_h < 200:
            popup_h = 550
        self._dialog.resize(self._dialog.width(), popup_h)
        self._dialog.move(btn_global.x(), btn_global.y() - popup_h - 4)


# ── DualStageSequencer ────────────────────────────────────────────────────────

class StageSequenceEditor(QWidget):
    """
    Editor for one stage's angle sequence.
    Supports two modes:
      • Sweep  – start / stop / step
      • Manual – comma-separated list of angles
    """

    sequence_changed = pyqtSignal()

    def __init__(self, label: str, default_step: float = 10.0, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Mode toggle
        mode_row = QHBoxLayout()
        self._sweep_radio  = QRadioButton("Sweep")
        self._manual_radio = QRadioButton("Manual")
        self._sweep_radio.setChecked(True)
        mode_group = QButtonGroup(self)
        mode_group.addButton(self._sweep_radio)
        mode_group.addButton(self._manual_radio)
        self._sweep_radio.toggled.connect(self._on_mode_change)
        mode_row.addWidget(self._sweep_radio)
        mode_row.addWidget(self._manual_radio)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # ── Sweep controls ──────────────────────────────────────────
        self._sweep_widget = QWidget()
        sweep_form = QFormLayout(self._sweep_widget)
        sweep_form.setContentsMargins(0, 0, 0, 0)
        sweep_form.setSpacing(3)

        self._start_spin = QDoubleSpinBox()
        self._start_spin.setRange(-720, 720); self._start_spin.setDecimals(3)
        self._start_spin.setValue(0.0)
        self._stop_spin  = QDoubleSpinBox()
        self._stop_spin.setRange(-720, 720);  self._stop_spin.setDecimals(3)
        self._stop_spin.setValue(90.0)
        self._step_spin  = QDoubleSpinBox()
        self._step_spin.setRange(0.001, 720); self._step_spin.setDecimals(3)
        self._step_spin.setValue(default_step)

        for spin in (self._start_spin, self._stop_spin, self._step_spin):
            spin.valueChanged.connect(self.sequence_changed)

        sweep_form.addRow("Start (°):", self._start_spin)
        sweep_form.addRow("Stop (°):",  self._stop_spin)
        sweep_form.addRow("Step (°):",  self._step_spin)
        layout.addWidget(self._sweep_widget)

        # ── Manual controls ─────────────────────────────────────────
        self._manual_widget = QWidget()
        manual_layout = QVBoxLayout(self._manual_widget)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(3)
        manual_layout.addWidget(QLabel("Angles (comma-separated °):"))
        self._manual_edit = QLineEdit("0, 30, 60, 90")
        self._manual_edit.setPlaceholderText("e.g. 0, 15, 30, 45, 90")
        self._manual_edit.textChanged.connect(self.sequence_changed)
        manual_layout.addWidget(self._manual_edit)
        self._manual_widget.setVisible(False)
        layout.addWidget(self._manual_widget)

    def _on_mode_change(self):
        sweep = self._sweep_radio.isChecked()
        self._sweep_widget.setVisible(sweep)
        self._manual_widget.setVisible(not sweep)
        self.sequence_changed.emit()

    def angles(self) -> list[float]:
        """Return the list of angles defined by the current mode."""
        if self._sweep_radio.isChecked():
            start = self._start_spin.value()
            stop  = self._stop_spin.value()
            step  = self._step_spin.value()
            if step <= 0:
                return []
            result = []
            v = start
            if stop >= start:
                while v <= stop + 1e-9:
                    result.append(round(v, 6))
                    v += step
            else:
                step = -abs(step)
                while v >= stop - 1e-9:
                    result.append(round(v, 6))
                    v += step
            return result
        else:
            raw = self._manual_edit.text()
            out = []
            for tok in raw.split(","):
                tok = tok.strip()
                if tok:
                    try:
                        out.append(float(tok))
                    except ValueError:
                        pass
            return out


class DualStageSequencer(QWidget):
    """
    Widget that defines independent angle sequences for the OptoSigma and
    Thorlabs stages, pairs them up, previews the result, and queues all
    scans in one click.

    Pairing modes
    ─────────────
    • Zip      – step both simultaneously (stops at the shorter list).
    • Product  – every (opto, thorlabs) combination (Cartesian product).
    • Opto only / Thorlabs only – ignore the other stage's list.
    """

    run_requested = pyqtSignal(list)   # emits list of (opto_angle, tl_angle) tuples

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(6)

        # ── Top: two side-by-side editors ───────────────────────────
        editors_row = QHBoxLayout()

        opto_group = QGroupBox("OptoSigma Angles")
        opto_vbox  = QVBoxLayout(opto_group)
        self._opto_editor = StageSequenceEditor("OptoSigma", default_step=10.0)
        self._opto_editor.sequence_changed.connect(self._refresh_preview)
        opto_vbox.addWidget(self._opto_editor)

        tl_group  = QGroupBox("Thorlabs Angles")
        tl_vbox   = QVBoxLayout(tl_group)
        self._tl_editor = StageSequenceEditor("Thorlabs", default_step=10.0)
        self._tl_editor.sequence_changed.connect(self._refresh_preview)
        tl_vbox.addWidget(self._tl_editor)

        editors_row.addWidget(opto_group)
        editors_row.addWidget(tl_group)
        outer.addLayout(editors_row)

        # ── Pairing mode ─────────────────────────────────────────────
        pair_row = QHBoxLayout()
        pair_row.addWidget(QLabel("Pair mode:"))
        self._pair_combo = QComboBox()
        self._pair_combo.addItems(["Zip (simultaneous)",
                                   "OptoSigma only", "Thorlabs only"])
        self._pair_combo.currentIndexChanged.connect(self._refresh_preview)
        pair_row.addWidget(self._pair_combo)

        self._scans_spin = QSpinBox()
        self._scans_spin.setRange(1, 100)
        self._scans_spin.setValue(1)
        self._scans_spin.valueChanged.connect(self._refresh_preview)
        pair_row.addWidget(QLabel("  Scans/step:"))
        pair_row.addWidget(self._scans_spin)
        pair_row.addStretch()
        outer.addLayout(pair_row)

        # ── Preview table ─────────────────────────────────────────────
        outer.addWidget(QLabel("Sequence preview  (each row = one set of scans):"))
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Step", "OptoSigma (°)", "Thorlabs (°)"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setMaximumHeight(150)
        outer.addWidget(self._table)

        self._step_count_label = QLabel("0 steps · 0 total scans")
        self._step_count_label.setStyleSheet("color: #555; font-style: italic;")
        outer.addWidget(self._step_count_label)

        # ── Run button ────────────────────────────────────────────────
        self._run_btn = QPushButton("▶  Run Dual Sequence")
        self._run_btn.setMinimumHeight(36)
        self._run_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._run_btn.setStyleSheet("""
            QPushButton {
                background-color: #2e7d32; color: white;
                font-weight: bold; padding: 6px 14px;
                border-radius: 4px; border: none;
            }
            QPushButton:hover  { background-color: #1b5e20; }
            QPushButton:disabled { background-color: #aaa; }
        """)
        self._run_btn.clicked.connect(self._on_run)
        outer.addWidget(self._run_btn)

        self._refresh_preview()

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_steps(self) -> list[tuple[float, float]]:
        """Return the list of (opto_angle, tl_angle) step pairs."""
        opto_angles = self._opto_editor.angles()
        tl_angles   = self._tl_editor.angles()
        mode = self._pair_combo.currentIndex()

        if mode == 0:   # Zip
            return list(zip(opto_angles, tl_angles))
        elif mode == 1: # OptoSigma only
            tl_fixed = tl_angles[0] if tl_angles else 0.0
            return [(o, tl_fixed) for o in opto_angles]
        else:           # Thorlabs only
            opto_fixed = opto_angles[0] if opto_angles else 0.0
            return [(opto_fixed, t) for t in tl_angles]

    def _refresh_preview(self):
        steps = self._build_steps()
        scans = self._scans_spin.value()
        self._table.setRowCount(len(steps))
        for row, (o, t) in enumerate(steps):
            self._table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
            self._table.setItem(row, 1, QTableWidgetItem(f"{o:.3f}"))
            self._table.setItem(row, 2, QTableWidgetItem(f"{t:.3f}"))
            # Alternate row shading
            color = QColor("#f5f5f5") if row % 2 == 0 else QColor("#ffffff")
            for col in range(3):
                self._table.item(row, col).setBackground(color)
        total = len(steps) * scans
        self._step_count_label.setText(
            f"{len(steps)} step{'s' if len(steps) != 1 else ''} · "
            f"{total} total scan{'s' if total != 1 else ''}"
        )
        self._run_btn.setEnabled(len(steps) > 0)

    def _on_run(self):
        steps = self._build_steps()
        if not steps:
            QMessageBox.warning(self, "Dual Sequence", "No steps to run.")
            return
        self.run_requested.emit(steps)

    def scans_per_step(self) -> int:
        return self._scans_spin.value()


# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(ManagedWindow):

    temp_updated_signal         = pyqtSignal(float)
    angle_updated_signal        = pyqtSignal(float)
    thorlabs_angle_updated_signal = pyqtSignal(float)

    def __init__(self):
        super().__init__(
            procedure_class=HoribaSpectrumProcedure,
            inputs=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed',
                'ccd_y_origin', 'ccd_y_size', 'ccd_x_bin',
            ],
            displays=[
                'excitation_wavelength', 'center_wavelength', 'exposure',
                'slit_position', 'gain', 'speed',
                'ccd_y_origin', 'ccd_y_size', 'ccd_x_bin',
            ],
            x_axis='Wavelength',
            y_axis='Intensity',
            sequencer=False,
        )
        self.setWindowTitle('Horiba Spectrum Scan')
        self.setMinimumSize(1200, 800)

        # Connect cross-thread signals
        self.temp_updated_signal.connect(self.on_temp_ui_update)
        self.angle_updated_signal.connect(self.on_angle_ui_update)
        self.thorlabs_angle_updated_signal.connect(self.on_thorlabs_angle_ui_update)

        self.loop = None
        self.loop_thread = None
        self._start_event_loop()

        # ── Controller (Thorlabs disabled by default) ─────────────────
        self.controller = HoribaController(enable_logging=True)

        try:
            self.run_async_task(self.controller.connect_hardware())
        except Exception as e:
            logger.error(f"Hardware connection failed: {e}")
            QMessageBox.critical(self, "Connection Error",
                                 f"Failed to connect to hardware:\n{e}")

        # ══════════════════════════════════════════════════════════════
        # ROW 1 — Grating  +  Scan count  (side by side)
        # ══════════════════════════════════════════════════════════════
        row1 = QWidget()
        row1_layout = QHBoxLayout(row1)
        row1_layout.setContentsMargins(0, 0, 0, 0)
        row1_layout.setSpacing(6)

        # ── Grating ───────────────────────────────────────────────────
        grating_widget = QGroupBox("Grating")
        grating_layout = QFormLayout()
        grating_layout.setContentsMargins(6, 6, 6, 6)
        self.grating_combo = QComboBox()
        self.grating_combo.addItems(GRATING_CHOICES.keys())
        self.grating_combo.setCurrentText('Third (150 grooves/mm)')
        self.grating_combo.currentTextChanged.connect(self.update_grating)
        grating_layout.addRow("Grating:", self.grating_combo)
        grating_widget.setLayout(grating_layout)

        # ── Scan count ────────────────────────────────────────────────
        scan_count_widget = QGroupBox("Scan Sequence")
        scan_count_layout = QFormLayout()
        scan_count_layout.setContentsMargins(6, 6, 6, 6)
        self.scans_per_angle_input = QtWidgets.QSpinBox()
        self.scans_per_angle_input.setRange(1, 100)
        self.scans_per_angle_input.setValue(1)
        scan_count_layout.addRow("Scans/Angle:", self.scans_per_angle_input)
        scan_count_widget.setLayout(scan_count_layout)

        row1_layout.addWidget(grating_widget, stretch=3)
        row1_layout.addWidget(scan_count_widget, stretch=2)

        # ══════════════════════════════════════════════════════════════
        # ROW 2 — Rotation stages in a tab widget
        # ══════════════════════════════════════════════════════════════
        stages_group = QGroupBox("Rotation Stages")
        stages_outer = QVBoxLayout(stages_group)
        stages_outer.setContentsMargins(4, 4, 4, 4)

        stage_tabs = QTabWidget()
        stage_tabs.setDocumentMode(True)

        # ── Tab 1: OptoSigma ──────────────────────────────────────────
        opto_tab = QWidget()
        opto_layout = QGridLayout(opto_tab)
        opto_layout.setContentsMargins(6, 6, 6, 6)
        opto_layout.setSpacing(4)

        self.current_angle_display = QLabel("Current: --.-°")
        self.current_angle_display.setStyleSheet("font-weight: bold;")
        self.refresh_angle_button = QPushButton("Refresh")
        self.refresh_angle_button.setFixedWidth(70)
        self.refresh_angle_button.clicked.connect(self.update_current_angle)

        opto_layout.addWidget(self.current_angle_display,  0, 0, 1, 2)
        opto_layout.addWidget(self.refresh_angle_button,   0, 2)

        self.set_angle_input = QDoubleSpinBox()
        self.set_angle_input.setRange(-360.0, 360.0)
        self.set_angle_input.setDecimals(2)
        self.set_angle_input.setValue(self.controller.last_angle)
        self.go_to_angle_button = QPushButton("Go")
        self.go_to_angle_button.setFixedWidth(50)
        self.go_to_angle_button.clicked.connect(self.do_go_to_angle)

        opto_layout.addWidget(QLabel("Target (°):"),       1, 0)
        opto_layout.addWidget(self.set_angle_input,        1, 1)
        opto_layout.addWidget(self.go_to_angle_button,     1, 2)

        self.return_to_origin_button = QPushButton("Return to Origin (0°)")
        self.return_to_origin_button.clicked.connect(self.do_return_to_origin)
        opto_layout.addWidget(self.return_to_origin_button, 2, 0, 1, 3)

        opto_layout.setColumnStretch(1, 1)

        # ── Tab 2: Thorlabs ───────────────────────────────────────────
        tl_tab = QWidget()
        tl_layout = QGridLayout(tl_tab)
        tl_layout.setContentsMargins(6, 6, 6, 6)
        tl_layout.setSpacing(4)

        # Status (auto-managed, no manual connect/disconnect)
        self.thorlabs_status_label = QLabel("Status: initializing…")
        self.thorlabs_status_label.setStyleSheet("color: orange;")
        tl_layout.addWidget(self.thorlabs_status_label,      0, 0, 1, 3)

        # Current angle
        self.thorlabs_angle_display = QLabel("Current: --.-°")
        self.thorlabs_angle_display.setStyleSheet("font-weight: bold;")
        self.thorlabs_refresh_button = QPushButton("Refresh")
        self.thorlabs_refresh_button.setFixedWidth(70)
        self.thorlabs_refresh_button.clicked.connect(self.update_thorlabs_angle)

        tl_layout.addWidget(self.thorlabs_angle_display,     1, 0, 1, 2)
        tl_layout.addWidget(self.thorlabs_refresh_button,    1, 2)

        # Target + go
        self.thorlabs_angle_input = QDoubleSpinBox()
        self.thorlabs_angle_input.setRange(0.0, 360.0)
        self.thorlabs_angle_input.setDecimals(3)
        self.thorlabs_angle_input.setValue(0.0)
        self.thorlabs_go_button = QPushButton("Go")
        self.thorlabs_go_button.setFixedWidth(50)
        self.thorlabs_go_button.clicked.connect(self.do_thorlabs_go_to_angle)

        tl_layout.addWidget(QLabel("Target (°):"),           2, 0)
        tl_layout.addWidget(self.thorlabs_angle_input,       2, 1)
        tl_layout.addWidget(self.thorlabs_go_button,         2, 2)

        self.thorlabs_home_button = QPushButton("Home Stage")
        self.thorlabs_home_button.clicked.connect(self.do_thorlabs_home)
        tl_layout.addWidget(self.thorlabs_home_button,        3, 0, 1, 3)

        tl_layout.setColumnStretch(1, 1)

        stage_tabs.addTab(opto_tab, "OptoSigma")
        stage_tabs.addTab(tl_tab,   "Thorlabs K10CR2")
        stages_outer.addWidget(stage_tabs)

        # Auto-update UI based on controller's Thorlabs state
        if (self.controller.thorlabs_stage is not None
                and self.controller.thorlabs_stage.is_connected):
            self._thorlabs_connect_ok()
        elif self.controller.enable_thorlabs_stage:
            # Was enabled but failed to connect during init
            self._thorlabs_show_failed()
        else:
            self.thorlabs_status_label.setText("Status: not available")
            self.thorlabs_status_label.setStyleSheet("color: grey;")

        # ══════════════════════════════════════════════════════════════
        # Assemble controls pane
        # ══════════════════════════════════════════════════════════════
        self.inputs.layout().insertWidget(0, row1)
        self.inputs.layout().addWidget(stages_group)

        self.setup_tools_ui()
        self.inputs.layout().addWidget(self.tools_group)

        self.file_input.extensions = ['csv']

        # Insert sequencer below the Queue/Abort buttons after pymeasure
        # has finished building the rest of the panel.
        self._dual_seq = DualStageSequencer()
        self._dual_seq.run_requested.connect(self._on_dual_sequence_run)
        self._dual_seq_section = PopupSequencerButton(self._dual_seq)
        QTimer.singleShot(0, self._insert_sequencer_at_bottom)

        self.update_current_angle()

        # Temperature polling timer
        self._temp_pending = False   # prevents stacking temp requests
        self.temp_timer = QTimer()
        self.temp_timer.timeout.connect(self.trigger_temperature_update)
        self.temp_timer.start(5000)

        # Resume the temp poll once a queued experiment finishes or is
        # aborted. Pymeasure's Manager exposes these as Qt signals.
        try:
            self.manager.finished.connect(lambda *_: self._resume_temp_poll())
            self.manager.aborted.connect(lambda *_: self._resume_temp_poll())
        except AttributeError:
            # Older pymeasure without these signals — fall back to the
            # 5 s timer reactivating itself the next time it ticks.
            pass

    # ── Tools UI ──────────────────────────────────────────────────────

    def setup_tools_ui(self):
        self.tools_group = QGroupBox("Tools")
        tools_layout = QHBoxLayout()
        tools_layout.setContentsMargins(6, 6, 6, 6)
        tools_layout.setSpacing(8)

        self.temp_label = QLabel("CCD Temp: -- °C")
        self.temp_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #333;")
        tools_layout.addWidget(self.temp_label, stretch=1)

        self.btn_rtc   = QPushButton("RTC")
        self.btn_rtc.clicked.connect(self._open_rtc_window)
        self.btn_image = QPushButton("Image Scan")
        self.btn_image.clicked.connect(self._open_image_window)
        tools_layout.addWidget(self.btn_rtc)
        tools_layout.addWidget(self.btn_image)

        self.tools_group.setLayout(tools_layout)

        # Lazy handles to in-process child windows.
        self._rtc_win = None
        self._image_win = None

    def _insert_sequencer_at_bottom(self):
        """Append the dual-stage sequencer below the Queue/Abort buttons."""
        # Walk up from file_input to find the nearest QVBoxLayout that
        # contains it, then append our section at the end.
        parent = self.file_input.parent()
        while parent is not None:
            layout = parent.layout()
            if layout is not None and isinstance(layout, QVBoxLayout):
                layout.addWidget(self._dual_seq_section)
                return
            parent = parent.parent()
        # Fallback: just add to inputs
        self.inputs.layout().addWidget(self._dual_seq_section)

    # ── CCD temperature ───────────────────────────────────────────────

    def trigger_temperature_update(self):
        if not self.controller or not self.controller.is_connected:
            self.temp_label.setText("CCD Temp: Disconnected")
            return
        if hasattr(self, 'manager') and self.manager.is_running():
            # Suspend rather than no-op: the queued scan and the temp
            # poll otherwise race for the single ICL websocket.
            self.temp_timer.stop()
            return
        # Don't stack another request if the previous one hasn't returned yet
        if self._temp_pending:
            return
        self._temp_pending = True
        future = asyncio.run_coroutine_threadsafe(
            self.controller.get_ccd_temperature(), self.loop
        )
        future.add_done_callback(self._handle_temp_result)

    def _resume_temp_poll(self):
        """Restart the 5 s temperature poll if it was paused."""
        if hasattr(self, "temp_timer") and not self.temp_timer.isActive():
            self.temp_timer.start(5000)

    def _handle_temp_result(self, fut):
        try:
            try:
                temp = fut.result()
                self.temp_updated_signal.emit(temp)
            except Exception:
                self.temp_updated_signal.emit(-999.0)
        finally:
            # finally: even if an unexpected error escapes the inner
            # try, the pending flag must reset — otherwise temp polls
            # silently stop forever after the first orphaned future.
            self._temp_pending = False

    def on_temp_ui_update(self, temp):
        if temp == -999.0:
            self.temp_label.setText("CCD Temp: Err")
        else:
            color = "green" if temp < -50 else "red"
            self.temp_label.setText(
                f"CCD Temp: <font color='{color}'>{temp:.1f} °C</font>"
            )

    # ── OptoSigma angle control ───────────────────────────────────────

    def update_current_angle(self):
        future = asyncio.run_coroutine_threadsafe(
            self.controller.get_rotation_angle(), self.loop
        )
        future.add_done_callback(self._handle_angle_result)

    def _handle_angle_result(self, fut):
        try:
            angle = fut.result()
            logger.info(f"Fetched angle from hardware: {angle:.2f}°") 
            self.angle_updated_signal.emit(angle)
        except Exception as e:
            logger.error(f"OptoSigma angle fetch error: {e}")

    def on_angle_ui_update(self, angle):
        self.current_angle_display.setText(f"Current Angle: {angle:.2f}°")
        self.set_angle_input.setValue(angle)

    def do_go_to_angle(self):
        target = self.set_angle_input.value()

        async def _set_and_update():
            await self.controller.set_rotation_angle(target)
            await asyncio.sleep(0.5)
            return await self.controller.get_rotation_angle()

        future = asyncio.run_coroutine_threadsafe(_set_and_update(), self.loop)
        future.add_done_callback(self._handle_angle_result)

    def do_return_to_origin(self):
        async def _home_and_update():
            await self.controller.return_rotation_to_origin()
            return await self.controller.get_rotation_angle()

        future = asyncio.run_coroutine_threadsafe(_home_and_update(), self.loop)
        future.add_done_callback(self._handle_angle_result)

    # ── Thorlabs angle control ────────────────────────────────────────

    def _thorlabs_connect_ok(self):
        self.thorlabs_status_label.setText("Status: connected ✓")
        self.thorlabs_status_label.setStyleSheet("color: green;")
        self.update_thorlabs_angle()

    def _thorlabs_show_disconnected(self):
        self.thorlabs_status_label.setText("Status: disconnected – reconnecting…")
        self.thorlabs_status_label.setStyleSheet("color: orange;")
        self.thorlabs_angle_display.setText("Current: --.-°")

    def _thorlabs_show_failed(self):
        self.thorlabs_status_label.setText("Status: not connected")
        self.thorlabs_status_label.setStyleSheet("color: red;")
        self.thorlabs_angle_display.setText("Current: --.-°")

    def update_thorlabs_angle(self):
        if not self.controller.enable_thorlabs_stage:
            return
        future = asyncio.run_coroutine_threadsafe(
            self.controller.get_thorlabs_angle(), self.loop
        )
        future.add_done_callback(self._handle_thorlabs_angle_result)

    def _handle_thorlabs_angle_result(self, fut):
        try:
            angle = fut.result()
            self.thorlabs_angle_updated_signal.emit(angle)
            # If we got a valid angle back, the stage is alive
            QTimer.singleShot(0, self._thorlabs_connect_ok)
        except Exception as e:
            logger.error(f"Thorlabs angle fetch error: {e}")
            QTimer.singleShot(0, self._thorlabs_show_failed)

    def on_thorlabs_angle_ui_update(self, angle):
        self.thorlabs_angle_display.setText(f"Current Angle: {angle:.3f}°")
        self.thorlabs_angle_input.setValue(angle)

    def do_thorlabs_go_to_angle(self):
        if not self.controller.enable_thorlabs_stage:
            QMessageBox.information(self, "Thorlabs", "Thorlabs stage is not enabled.")
            return
        target = self.thorlabs_angle_input.value()
        self.thorlabs_status_label.setText("Status: moving…")
        self.thorlabs_status_label.setStyleSheet("color: orange;")

        async def _move_and_update():
            await self.controller.set_thorlabs_angle(target)
            return await self.controller.get_thorlabs_angle()

        future = asyncio.run_coroutine_threadsafe(_move_and_update(), self.loop)
        future.add_done_callback(self._handle_thorlabs_angle_result)

    def do_thorlabs_home(self):
        if not self.controller.enable_thorlabs_stage:
            QMessageBox.information(self, "Thorlabs", "Thorlabs stage is not enabled.")
            return
        self.thorlabs_status_label.setText("Status: homing…")
        self.thorlabs_status_label.setStyleSheet("color: orange;")

        async def _home_and_update():
            await self.controller.home_thorlabs_stage()
            return await self.controller.get_thorlabs_angle()

        future = asyncio.run_coroutine_threadsafe(_home_and_update(), self.loop)
        future.add_done_callback(self._handle_thorlabs_angle_result)

    # ── In-process child windows ──────────────────────────────────────

    def _open_rtc_window(self):
        """Open the live-view (RTC) window in-process, sharing the
        controller and event loop. No subprocess, no ICL teardown."""
        from rtc import LiveViewWindow

        if self._rtc_win is None:
            self._rtc_win = LiveViewWindow(
                controller=self.controller, loop=self.loop, parent=self,
            )
            self._rtc_win.scanning_changed.connect(self._on_child_scanning_changed)
            # Forget the handle once the user actually closes it so a
            # later click rebuilds with fresh state.
            self._rtc_win.destroyed.connect(lambda *_: self._on_child_destroyed("rtc"))
        self._rtc_win.show()
        self._rtc_win.raise_()
        self._rtc_win.activateWindow()

    def _open_image_window(self):
        """Open the image-mode window in-process, sharing the controller
        and event loop."""
        from image import ImageWindow

        if self._image_win is None:
            self._image_win = ImageWindow(
                controller=self.controller, loop=self.loop, parent=self,
            )
            self._image_win.destroyed.connect(lambda *_: self._on_child_destroyed("image"))
        self._image_win.show()
        self._image_win.raise_()
        self._image_win.activateWindow()

    def _on_child_destroyed(self, which: str) -> None:
        if which == "rtc":
            self._rtc_win = None
        elif which == "image":
            self._image_win = None

    def _on_child_scanning_changed(self, busy: bool) -> None:
        """Disable the Queue/inputs panel while a live RTC scan runs.

        We block the inputs widget rather than poking pymeasure's
        internal queue button so this works regardless of where
        ManagedWindow placed the button. The CCD temperature poll
        also pauses while the child is acquiring — overlapping SDK
        calls on the single ICL websocket can deadlock.
        """
        self.inputs.setEnabled(not busy)
        if busy:
            if hasattr(self, "temp_timer"):
                self.temp_timer.stop()
        else:
            self._resume_temp_poll()

    # ── Event loop helpers ────────────────────────────────────────────

    def _start_event_loop(self):
        def run_loop(loop):
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(
            target=run_loop, args=(self.loop,), daemon=True
        )
        self.loop_thread.start()

    def run_async_task(self, task, timeout=30):
        try:
            future = asyncio.run_coroutine_threadsafe(task, self.loop)
            return future.result(timeout=timeout)
        except Exception as e:
            logger.error(f"Error running async task: {e}")
            raise

    def update_grating(self, text):
        logger.info(f"Grating changed to {text}")

    # ── Procedure factory ─────────────────────────────────────────────

    def make_procedure(self, rotation_angle=None, thorlabs_angle=None):
        procedure = self.procedure_class()
        procedure.controller = self.controller
        procedure.loop = self.loop

        for param_name in [
            "excitation_wavelength", "center_wavelength", "exposure",
            "slit_position", "gain", "speed",
            "ccd_y_origin", "ccd_y_size", "ccd_x_bin",
        ]:
            if hasattr(self.inputs, param_name):
                value = getattr(self.inputs, param_name).value()
                setattr(procedure, param_name, value)

        if rotation_angle is not None:
            procedure.rotation_angle = rotation_angle
        else:
            procedure.rotation_angle = self.set_angle_input.value()

        procedure.grating = self.grating_combo.currentText()

        # thorlabs_angle: explicit override → widget value → current cached value
        if thorlabs_angle is not None:
            procedure.thorlabs_angle = thorlabs_angle
        else:
            procedure.thorlabs_angle = self.thorlabs_angle_input.value()

        return procedure

    # ── Queue ─────────────────────────────────────────────────────────

    def queue(self, procedure=None, rotation_angle=None, thorlabs_angle=None):
        if procedure is None:
            procedure = self.make_procedure(
                rotation_angle=rotation_angle,
                thorlabs_angle=thorlabs_angle,
            )

        scans_per_angle  = self.scans_per_angle_input.value()
        base_rotation    = procedure.rotation_angle
        base_thorlabs    = procedure.thorlabs_angle

        for i in range(1, scans_per_angle + 1):
            current_procedure = self.make_procedure(
                rotation_angle=base_rotation,
                thorlabs_angle=base_thorlabs,
            )
            current_procedure.scan_number = i

            filename = self.unique_filename(
                self.file_input.directory,
                self.file_input.filename,
                current_procedure.rotation_angle,
                current_procedure.thorlabs_angle,
                i,
            )
            current_procedure.data_filename = filename

            experiment = self.new_experiment(Results(current_procedure, filename))
            self.manager.queue(experiment)

        self.update_current_angle()
        sleep(0.5)

    def _on_dual_sequence_run(self, steps: list):
        """
        Queue one batch of scans for every (opto_angle, tl_angle) step pair.
        The DualStageSequencer's own scans_per_step overrides the single-scan
        widget for this batch.
        """
        scans_per_step = self._dual_seq.scans_per_step()

        for opto_angle, tl_angle in steps:
            for scan_i in range(1, scans_per_step + 1):
                procedure = self.make_procedure(
                    rotation_angle=opto_angle,
                    thorlabs_angle=tl_angle,
                )
                procedure.scan_number = scan_i

                filename = self.unique_filename(
                    self.file_input.directory,
                    self.file_input.filename,
                    opto_angle,
                    tl_angle,
                    scan_i,
                )
                procedure.data_filename = filename
                experiment = self.new_experiment(Results(procedure, filename))
                self.manager.queue(experiment)

        # Refresh angle displays after queuing
        self.update_current_angle()
        self.update_thorlabs_angle()

    def unique_filename(self, directory, base_filename, rotation_angle,
                        thorlabs_angle, scan_number):
        counter   = 1
        opto_str  = f"opto{rotation_angle:.1f}"
        tl_str    = f"thor{thorlabs_angle:.1f}"
        filename  = f"{base_filename}_{opto_str}_{tl_str}_S{scan_number}_{counter}.csv"
        file_path = os.path.join(directory, filename)

        while os.path.exists(file_path):
            counter  += 1
            filename  = f"{base_filename}_{opto_str}_{tl_str}_S{scan_number}_{counter}.csv"
            file_path = os.path.join(directory, filename)

        logger.info(f"Generated filename: {file_path}")
        return file_path

    def closeEvent(self, event):
        logger.info("Closing application...")
        if hasattr(self, '_is_closing') and self._is_closing:
            event.accept()
            return
        self._is_closing = True

        # Close any open child windows first; they share our controller
        # and loop and will skip their own shutdown because they don't
        # own them.
        for child in (self._rtc_win, self._image_win):
            if child is not None:
                try:
                    child.close()
                except Exception:
                    pass

        # Make sure no acquisition is in flight before shutting down.
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.controller.acquisition_abort(), self.loop
            )
            future.result(timeout=5)
        except Exception as e:
            logger.warning(f"acquisition_abort during shutdown failed: {e}")

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.controller.shutdown(), self.loop
            )
            future.result(timeout=10)
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