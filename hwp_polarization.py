#!/usr/bin/env python
"""HWP polarization calibration & dual-sequence angle-list generator.

GUI workflow (see docs/superpowers/specs/2026-07-16-hwp-polarization-design.md):
calibrate the incoming HWP (OptoSigma stage) and the outgoing HWP (Thorlabs
K10CR2) one at a time against a fixed polarizer + PM100A, persist each fit
to hwp_calibration.json, then generate co- and cross-polarized stage-angle
lists ready to paste into the Dual-Stage Synchronized Sequence manual boxes.

Physics: fit_waveplate locates each HWP's fast-axis stage angle z (peak,
mod 90 deg). A HWP at angle beta maps polarization p -> 2*beta - p, so to
rotate the incident polarization by phi and analyze it back onto the fixed
analyzer: incoming stage = z_in + phi/2; outgoing stage = z_out + phi/2
(co-polarized) or z_out + phi/2 + 45 deg (cross-polarized).

Run with:  uv run python hwp_polarization.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import time
from datetime import datetime

import numpy as np
import pyvisa
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from ThorlabsPM100 import ThorlabsPM100
from loguru import logger
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from optosigmacontroller import OptoSigmaController
from thorlabscontroller import ThorlabsK10CR2Controller, list_k10cr2_serials
from waveplate_scan import find_pm100, fit_waveplate, read_power


# ── pure sequence math ──────────────────────────────────────────────


def phi_values(start: float, stop: float, step: float) -> list[float]:
    """Inclusive-stop phi grid, same semantics as the sequencer's sweep
    editor: empty on step <= 0, descending allowed, 1e-9 end tolerance."""
    if step <= 0:
        return []
    out = []
    v = start
    if stop >= start:
        while v <= stop + 1e-9:
            out.append(round(v, 6))
            v += step
    else:
        step = -abs(step)
        while v >= stop - 1e-9:
            out.append(round(v, 6))
            v += step
    return out


def compute_hwp_sequence(
    z_in: float, z_out: float, phis: list[float]
) -> dict[str, tuple[list[float], list[float]]]:
    """Stage-angle lists for co- and cross-polarized runs.

    z_in / z_out: calibrated fast-axis stage angles (deg) of the incoming
    (OptoSigma) and outgoing (Thorlabs) HWPs. Returns
    {"co": (opto, tl), "cross": (opto, tl)} with one entry per phi.
    """
    opto = [z_in + p / 2.0 for p in phis]
    co_tl = [z_out + p / 2.0 for p in phis]
    cross_tl = [z_out + p / 2.0 + 45.0 for p in phis]
    return {"co": (opto, co_tl), "cross": (opto, cross_tl)}


def format_angle_line(angles: list[float]) -> str:
    """Comma-separated 3-decimal line, pasteable into the dual sequencer's
    Manual angle box (which splits on commas and float()s each token)."""
    return ", ".join(f"{a:.3f}" for a in angles)


# ── calibration records ─────────────────────────────────────────────

RECORDS_FILE = pathlib.Path(__file__).parent / "hwp_calibration.json"
ROLES = ("incoming", "outgoing")
ROLE_STAGE = {"incoming": "optosigma", "outgoing": "thorlabs"}


def load_records(path=None) -> dict:
    """Stored calibration records by role. Empty dict on missing or
    unreadable file — partial state (one role calibrated) is normal.

    path=None resolves to RECORDS_FILE at call time, so tests can repoint
    the module attribute without touching the real file."""
    path = pathlib.Path(path if path is not None else RECORDS_FILE)
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        return {k: v for k, v in data.items() if k in ROLES and isinstance(v, dict)}
    except Exception as e:
        logger.warning(f"could not read {path}: {e}; starting with no records")
        return {}


def save_record(role: str, record: dict, path=None) -> None:
    """Merge one role's record into the JSON file (atomic replace)."""
    path = pathlib.Path(path if path is not None else RECORDS_FILE)
    records = load_records(path)
    records[role] = record
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2))
    tmp.replace(path)


def make_record(stage: str, fit: dict, csv_path: str) -> dict:
    """Persistable record from a fit_waveplate result."""
    amp4 = fit["amp4"]
    return {
        "stage": stage,
        "peak_angle_deg": float(fit["peak_angle"]),
        "r2": float(fit["r2"]),
        "visibility": float(fit["visibility"]),
        "amp2_over_amp4": (float(fit["amp2"] / amp4) if amp4 else None),
        "csv_path": str(csv_path),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def parse_scan_csv(path) -> tuple[np.ndarray, np.ndarray]:
    """Read an angle/power calibration scan CSV (the waveplate_scan.py /
    calibrate-panel output format). Raises ValueError on the wrong shape."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows or [c.strip() for c in rows[0][:2]] != ["angle_deg", "power_W"]:
        raise ValueError(
            f"{path}: expected a CSV with header 'angle_deg,power_W' "
            f"(the calibration-scan output format)"
        )
    ang, pw = [], []
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip():
            ang.append(float(row[0]))
            pw.append(float(row[1]))
    return np.asarray(ang), np.asarray(pw)


# ── scan loop + worker ──────────────────────────────────────────────


def run_scan(stage, read_fn, targets, settle, on_point, should_stop):
    """Move-settle-read loop with injected hardware, so it is testable
    without devices. Returns (measured_angles, powers); stops early (keeping
    partial data) when should_stop() turns true."""
    measured, powers = [], []
    for target in targets:
        if should_stop():
            break
        stage.degree = float(target)
        if settle > 0:
            time.sleep(settle)
        power = read_fn()
        actual = stage.degree
        measured.append(actual)
        powers.append(power)
        on_point(actual, power)
    return measured, powers


class ScanWorker(QThread):
    """Owns the stage + power meter for exactly one calibration scan.

    Connects on start and always disconnects on the way out, so no hardware
    is held between scans — the GUI enforces one scan (one waveplate) at a
    time by keeping at most one worker alive.
    """

    point = pyqtSignal(float, float)
    finished_ok = pyqtSignal(list, list)
    failed = pyqtSignal(str)

    def __init__(self, role: str, params: dict, parent=None):
        super().__init__(parent)
        self._role = role
        self._params = params
        self._stop = False

    def request_stop(self):
        self._stop = True

    def _connect_stage(self):
        if self._role == "incoming":
            port = self._params["port"]
            stage = OptoSigmaController(port=port)
            if not stage.connect():
                raise RuntimeError(
                    f"failed to connect to OptoSigma on {port} — serial ports "
                    f"can't be shared; if the main GUI holds this stage, "
                    f"disconnect it there first"
                )
            return stage
        serial = self._params["serial"]
        if not serial:
            found = list_k10cr2_serials()
            if len(found) == 1:
                serial = found[0]
            elif not found:
                raise RuntimeError(
                    "no Thorlabs K10CR2 found — check the USB connection, or "
                    "enter a serial number"
                )
            else:
                raise RuntimeError(
                    f"multiple K10CR2 stages found: {found} — enter one explicitly"
                )
        stage = ThorlabsK10CR2Controller(serial_number=serial)
        if not stage.connect():
            raise RuntimeError(
                f"failed to connect to Thorlabs K10CR2 {serial} — if the main "
                f"GUI holds this stage, disconnect it there first"
            )
        if self._params["home"]:
            stage.home()
        return stage

    def run(self):
        stage = None
        inst = None
        try:
            rm = pyvisa.ResourceManager()
            resource = self._params["visa"] or find_pm100(rm)
            inst = rm.open_resource(resource)
            inst.timeout = 5000
            pm = ThorlabsPM100(inst)

            stage = self._connect_stage()

            p = self._params
            targets = np.arange(p["start"], p["stop"] + p["step"] / 2.0, p["step"])
            measured, powers = run_scan(
                stage,
                lambda: read_power(pm, p["navg"]),
                targets,
                p["settle"],
                lambda a, pw: self.point.emit(a, pw),
                lambda: self._stop,
            )
            self.finished_ok.emit(list(measured), list(powers))
        except Exception as e:  # every hardware error surfaces as a GUI dialog
            self.failed.emit(str(e))
        finally:
            if stage is not None:
                try:
                    stage.disconnect()
                except Exception:
                    pass
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass


# ── GUI ─────────────────────────────────────────────────────────────


def _dspin(lo: float, hi: float, val: float, dec: int = 3) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    return s


class HwpPolarizationWindow(QWidget):
    """Calibrate one HWP at a time, keep both records, emit angle lists."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("HWP Polarization Calibration")
        self._records = load_records()
        self._worker = None
        self._scan_role = "incoming"
        self._scan_angles: list[float] = []
        self._scan_powers: list[float] = []

        outer = QVBoxLayout(self)
        outer.addWidget(self._build_calibrate_group())
        outer.addWidget(self._build_records_group())
        self._seq_hint = QLabel(
            "Sequence lists unlock once BOTH waveplates have calibration records."
        )
        self._seq_hint.setStyleSheet("color: #a00; font-style: italic;")
        outer.addWidget(self._seq_hint)
        self._seq_group = self._build_sequence_group()
        outer.addWidget(self._seq_group)
        self._refresh_records_panel()

    # ── calibrate panel ──────────────────────────────────────────────

    def _build_calibrate_group(self) -> QGroupBox:
        group = QGroupBox("Calibrate (one waveplate at a time)")
        vbox = QVBoxLayout(group)

        role_row = QHBoxLayout()
        self._role_incoming = QRadioButton("Incoming HWP — OptoSigma")
        self._role_outgoing = QRadioButton("Outgoing HWP — Thorlabs")
        self._role_incoming.setChecked(True)
        self._role_incoming.toggled.connect(self._on_role_change)
        role_row.addWidget(self._role_incoming)
        role_row.addWidget(self._role_outgoing)
        role_row.addStretch()
        vbox.addLayout(role_row)

        self._opto_conn = QWidget()
        opto_form = QFormLayout(self._opto_conn)
        opto_form.setContentsMargins(0, 0, 0, 0)
        self._port_edit = QLineEdit("COM3")
        opto_form.addRow("OptoSigma port:", self._port_edit)
        vbox.addWidget(self._opto_conn)

        self._tl_conn = QWidget()
        tl_form = QFormLayout(self._tl_conn)
        tl_form.setContentsMargins(0, 0, 0, 0)
        self._serial_edit = QLineEdit()
        self._serial_edit.setPlaceholderText("blank = auto-detect")
        self._home_check = QCheckBox("Home before scan")
        tl_form.addRow("K10CR2 serial:", self._serial_edit)
        tl_form.addRow("", self._home_check)
        self._tl_conn.setVisible(False)
        vbox.addWidget(self._tl_conn)

        params_form = QFormLayout()
        self._visa_edit = QLineEdit()
        self._visa_edit.setPlaceholderText("blank = auto-detect PM100")
        self._start_spin = _dspin(-720, 720, 0.0)
        self._stop_spin = _dspin(-720, 720, 180.0)
        self._step_spin = _dspin(0.001, 720, 2.0)
        self._settle_spin = _dspin(0.0, 10.0, 0.3, dec=2)
        self._navg_spin = QSpinBox()
        self._navg_spin.setRange(1, 1000)
        self._navg_spin.setValue(5)
        params_form.addRow("PM100 VISA:", self._visa_edit)
        params_form.addRow("Start (°):", self._start_spin)
        params_form.addRow("Stop (°):", self._stop_spin)
        params_form.addRow("Step (°):", self._step_spin)
        params_form.addRow("Settle (s):", self._settle_spin)
        params_form.addRow("Reads to average:", self._navg_spin)
        vbox.addLayout(params_form)

        btn_row = QHBoxLayout()
        self._run_btn = QPushButton("▶  Run Calibration")
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._stop_btn)
        vbox.addLayout(btn_row)

        self._fig = Figure(figsize=(5, 3), tight_layout=True)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._ax = self._fig.add_subplot(111)
        self._reset_axes()
        vbox.addWidget(self._canvas)

        self._fit_label = QLabel("no fit yet")
        self._fit_label.setWordWrap(True)
        vbox.addWidget(self._fit_label)
        return group

    def _reset_axes(self):
        self._ax.clear()
        self._ax.set_xlabel("stage angle (deg)")
        self._ax.set_ylabel("power (W)")

    def _role(self) -> str:
        return "incoming" if self._role_incoming.isChecked() else "outgoing"

    def _on_role_change(self):
        incoming = self._role_incoming.isChecked()
        self._opto_conn.setVisible(incoming)
        self._tl_conn.setVisible(not incoming)

    def _set_scanning(self, scanning: bool):
        self._run_btn.setEnabled(not scanning)
        self._stop_btn.setEnabled(scanning)
        for w in (
            self._role_incoming,
            self._role_outgoing,
            self._opto_conn,
            self._tl_conn,
            self._visa_edit,
            self._start_spin,
            self._stop_spin,
            self._step_spin,
            self._settle_spin,
            self._navg_spin,
            self._load_buttons["incoming"],
            self._load_buttons["outgoing"],
        ):
            w.setEnabled(not scanning)

    def _on_run(self):
        self._scan_role = self._role()
        params = {
            "visa": self._visa_edit.text().strip() or None,
            "port": self._port_edit.text().strip(),
            "serial": self._serial_edit.text().strip() or None,
            "home": self._home_check.isChecked(),
            "start": self._start_spin.value(),
            "stop": self._stop_spin.value(),
            "step": self._step_spin.value(),
            "settle": self._settle_spin.value(),
            "navg": self._navg_spin.value(),
        }
        self._scan_angles, self._scan_powers = [], []
        self._reset_axes()
        (self._live_line,) = self._ax.plot([], [], "o-", ms=4)
        self._canvas.draw_idle()

        self._worker = ScanWorker(self._scan_role, params)
        self._worker.point.connect(self._on_point)
        self._worker.finished_ok.connect(self._on_scan_done)
        self._worker.failed.connect(self._on_scan_failed)
        self._set_scanning(True)
        self._worker.start()

    def _on_stop(self):
        if self._worker is not None:
            self._worker.request_stop()

    def _on_point(self, angle: float, power: float):
        self._scan_angles.append(angle)
        self._scan_powers.append(power)
        self._live_line.set_data(self._scan_angles, self._scan_powers)
        self._ax.relim()
        self._ax.autoscale_view()
        self._canvas.draw_idle()

    def _on_scan_failed(self, message: str):
        self._set_scanning(False)
        self._worker = None
        QMessageBox.critical(self, "Calibration", message)

    def _on_scan_done(self, angles: list, powers: list):
        self._set_scanning(False)
        self._worker = None
        role = self._scan_role
        if not angles:
            QMessageBox.warning(self, "Calibration", "No data collected.")
            return
        csv_path = pathlib.Path(__file__).parent / (
            f"hwp_calib_{role}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        )
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["angle_deg", "power_W"])
            w.writerows(zip(angles, powers))
        if len(angles) < 6:
            QMessageBox.warning(
                self,
                "Calibration",
                f"Only {len(angles)} points — saved {csv_path.name} but that "
                f"is too few to fit. Record not updated.",
            )
            return
        self._fit_and_store(role, np.asarray(angles), np.asarray(powers), csv_path)

    def _fit_and_store(self, role, angles, powers, csv_path):
        fit = fit_waveplate(angles, powers)
        save_record(role, make_record(ROLE_STAGE[role], fit, str(csv_path)))
        self._records = load_records()
        self._refresh_records_panel()
        self._show_fit(role, angles, powers, fit)

    def _show_fit(self, role, angles, powers, fit):
        self._reset_axes()
        self._ax.plot(angles, powers, "o", ms=4, label="data")
        fine = np.linspace(angles.min(), angles.max(), 500)
        t = np.deg2rad(fine)
        model = (
            fit["C"]
            + fit["a4"] * np.cos(4 * t)
            + fit["b4"] * np.sin(4 * t)
            + fit["a2"] * np.cos(2 * t)
            + fit["b2"] * np.sin(2 * t)
        )
        # clip at the detected floor so the curve hugs clipped minima
        self._ax.plot(fine, np.maximum(fit["floor"], model), "-", lw=1.5, label="fit")
        self._ax.legend()
        self._canvas.draw_idle()
        ratio = fit["amp2"] / fit["amp4"] if fit["amp4"] else float("nan")
        clip = (
            f" — WARNING: {fit['n_clipped']}/{fit['n_total']} floored points excluded"
            if fit["n_clipped"]
            else ""
        )
        self._fit_label.setText(
            f"{role}: axis ∥ polarizer at {fit['peak_angle']:.2f}° (mod 90°), "
            f"R²={fit['r2']:.4f}, visibility={fit['visibility']:.3f}, "
            f"A2/A4={ratio:.2%}{clip}"
        )

    # ── records panel ────────────────────────────────────────────────

    def _build_records_group(self) -> QGroupBox:
        group = QGroupBox("Calibration records (hwp_calibration.json)")
        grid = QGridLayout(group)
        self._record_labels = {}
        self._load_buttons = {}
        for row, role in enumerate(ROLES):
            label = QLabel()
            label.setWordWrap(True)
            self._record_labels[role] = label
            btn = QPushButton("Load CSV…")
            btn.clicked.connect(lambda _, r=role: self._on_load_csv(r))
            self._load_buttons[role] = btn
            grid.addWidget(label, row, 0)
            grid.addWidget(btn, row, 1)
        grid.setColumnStretch(0, 1)
        return group

    def _refresh_records_panel(self):
        for role in ROLES:
            rec = self._records.get(role)
            if rec is None:
                text = f"{role}: not calibrated"
            else:
                text = (
                    f"{role} ({rec['stage']}): axis {rec['peak_angle_deg']:.2f}°, "
                    f"R²={rec['r2']:.4f}, vis={rec['visibility']:.3f} — "
                    f"calibrated {rec['timestamp']}  [{rec['csv_path']}]"
                )
            self._record_labels[role].setText(text)
        both = all(r in self._records for r in ROLES)
        self._seq_group.setEnabled(both)
        self._seq_hint.setVisible(not both)

    def _on_load_csv(self, role: str):
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Load calibration scan for {role} HWP",
            str(pathlib.Path(__file__).parent),
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        try:
            angles, powers = parse_scan_csv(path)
        except (ValueError, OSError) as e:
            QMessageBox.critical(self, "Load CSV", str(e))
            return
        if len(angles) < 6:
            QMessageBox.warning(
                self, "Load CSV", f"Only {len(angles)} points — too few to fit."
            )
            return
        self._fit_and_store(role, angles, powers, path)

    # ── sequence panel ───────────────────────────────────────────────

    def _build_sequence_group(self) -> QGroupBox:
        group = QGroupBox("Dual-sequence angle lists (paste into Manual boxes)")
        vbox = QVBoxLayout(group)

        form = QFormLayout()
        self._phi_start = _dspin(-720, 720, 0.0)
        self._phi_stop = _dspin(-720, 720, 180.0)
        self._phi_step = _dspin(0.001, 720, 15.0)
        form.addRow("φ start (°):", self._phi_start)
        form.addRow("φ stop (°):", self._phi_stop)
        form.addRow("φ step (°):", self._phi_step)
        vbox.addLayout(form)

        gen_btn = QPushButton("Generate lists")
        gen_btn.clicked.connect(self._on_generate)
        vbox.addWidget(gen_btn)

        self._seq_edits = {}
        for key, title in (
            ("co_opto", "Co — OptoSigma"),
            ("co_tl", "Co — Thorlabs"),
            ("cross_opto", "Cross — OptoSigma"),
            ("cross_tl", "Cross — Thorlabs"),
        ):
            row = QHBoxLayout()
            label = QLabel(title + ":")
            label.setMinimumWidth(130)
            row.addWidget(label)
            edit = QLineEdit()
            edit.setReadOnly(True)
            self._seq_edits[key] = edit
            row.addWidget(edit, stretch=1)
            copy_btn = QPushButton("Copy")
            copy_btn.clicked.connect(
                lambda _, e=edit: QApplication.clipboard().setText(e.text())
            )
            row.addWidget(copy_btn)
            vbox.addLayout(row)

        save_btn = QPushButton("Save .txt…")
        save_btn.clicked.connect(self._on_save_txt)
        vbox.addWidget(save_btn)
        return group

    def _on_generate(self):
        phis = phi_values(
            self._phi_start.value(), self._phi_stop.value(), self._phi_step.value()
        )
        if not phis:
            QMessageBox.warning(self, "Sequence", "φ range produced no angles.")
            return
        self._last_phis = phis
        seq = compute_hwp_sequence(
            self._records["incoming"]["peak_angle_deg"],
            self._records["outgoing"]["peak_angle_deg"],
            phis,
        )
        self._seq_edits["co_opto"].setText(format_angle_line(seq["co"][0]))
        self._seq_edits["co_tl"].setText(format_angle_line(seq["co"][1]))
        self._seq_edits["cross_opto"].setText(format_angle_line(seq["cross"][0]))
        self._seq_edits["cross_tl"].setText(format_angle_line(seq["cross"][1]))

    def _on_save_txt(self):
        if not self._seq_edits["co_opto"].text():
            QMessageBox.warning(self, "Sequence", "Generate the lists first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save angle lists",
            str(pathlib.Path(__file__).parent / "hwp_sequence.txt"),
            "Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        rec_in = self._records["incoming"]
        rec_out = self._records["outgoing"]
        lines = [
            "# HWP dual-sequence angle lists",
            f"# generated {datetime.now().isoformat(timespec='seconds')}",
            f"# incoming: axis {rec_in['peak_angle_deg']:.3f}° "
            f"({rec_in['timestamp']}, {rec_in['csv_path']})",
            f"# outgoing: axis {rec_out['peak_angle_deg']:.3f}° "
            f"({rec_out['timestamp']}, {rec_out['csv_path']})",
            f"# phi: {format_angle_line(self._last_phis)}",
            "",
            "[co-polarized run]",
            f"OptoSigma: {self._seq_edits['co_opto'].text()}",
            f"Thorlabs:  {self._seq_edits['co_tl'].text()}",
            "",
            "[cross-polarized run]",
            f"OptoSigma: {self._seq_edits['cross_opto'].text()}",
            f"Thorlabs:  {self._seq_edits['cross_tl'].text()}",
        ]
        pathlib.Path(path).write_text("\n".join(lines) + "\n")


def main():
    import sys

    app = QApplication(sys.argv)
    win = HwpPolarizationWindow()
    win.resize(680, 900)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
