# HWP Polarization Calibration & Sequence GUI — Design

**Date:** 2026-07-16
**New file:** `hwp_polarization.py` (standalone PyQt5 GUI script, repo root)
**Touched files:** none in `waveplate_scan.py` (imports only); new tests in `tests/`

## Purpose

One tool that owns the whole half-wave-plate (HWP) polarization workflow:

1. Calibrate the **incoming** HWP (OptoSigma stage) and the **outgoing** HWP
   (Thorlabs K10CR2) — strictly **one at a time**, since the physical
   calibration path (waveplate → fixed polarizer → PM100A) can only hold one
   waveplate.
2. Persist each fit as a calibration record so the tool "remembers" both.
3. Once both records exist, generate co-polarized and cross-polarized stage
   angle lists ready to paste into the Dual-Stage Synchronized Sequence
   manual-angle boxes in `horibagui.py`.

## Physics

`waveplate_scan.py`'s `fit_waveplate` locates each HWP's fast-axis stage
angle where the axis is parallel to the fixed polarizer: `peak_angle`,
mod 90° (fast vs slow axis is irrelevant for a HWP). Call these `z_in`
(incoming, OptoSigma) and `z_out` (outgoing, Thorlabs).

A HWP with fast axis at angle β maps polarization at angle p to `2β − p`.
To rotate the incident polarization by φ from the lab polarizer axis and
analyze it back onto the fixed analyzer:

- **Incoming stage:** `z_in + φ/2`
- **Outgoing stage, co-polarized (∥):** `z_out + φ/2`
- **Outgoing stage, cross-polarized (⊥):** `z_out + φ/2 + 45°`

φ values come from start/stop/step inputs (default 0–180° in 15° steps).
Output is **two separate runs** (per user decision): one all-co list, one
all-cross list. Angles are emitted as-is (no mod-360 wrap needed: with
φ ≤ 360 and z < 90 all values stay well inside the ±720° stage range).

## GUI layout (single window, three sections top to bottom)

### 1. Calibrate panel

- Role selector (radio): **Incoming HWP — OptoSigma** / **Outgoing HWP —
  Thorlabs**. Selecting a role switches the visible connection fields:
  - OptoSigma: COM port (default `COM3`).
  - Thorlabs: serial (blank = auto-detect via `list_k10cr2_serials`),
    "Home before scan" checkbox.
- Scan params, defaults matching `waveplate_scan.py`: start 0°, stop 180°,
  step 2°, settle 0.3 s, navg 5. PM100A VISA field (blank = auto-detect via
  `find_pm100`).
- **Run Calibration** button + **Stop** button. While a scan runs, the whole
  calibrate panel and the Load-CSV buttons are disabled — one calibration at
  a time is structurally enforced. Stop aborts cleanly and keeps partial
  data (fitted only if ≥ 6 points were collected — below that a fit is
  meaningless and the partial CSV is just saved).
- Embedded matplotlib canvas (`FigureCanvasQTAgg`): live power-vs-angle
  points during the scan; fit curve overlaid on completion (clipped at the
  detected floor, as in `waveplate_scan.py`).
- Fit summary label: peak angle, R², visibility, A2/A4 ratio, clipped-point
  warning when `n_clipped > 0`.

### 2. Records panel

- One row per role showing: peak angle (mod 90°), R², visibility, stage
  type, timestamp ("calibrated 2026-07-16 14:32"), and the raw-CSV path.
- **Load CSV…** button per role: pick any previous calibration scan CSV
  (`angle_deg,power_W` columns), re-fit with `fit_waveplate`, store as that
  role's record. Same code path as a fresh scan's fit step.
- Records persist to `hwp_calibration.json` next to the script and reload
  on startup.

### 3. Sequence panel

- Enabled only when both records exist (disabled with a hint label
  otherwise).
- φ start / stop / step spin boxes (defaults 0 / 180 / 15).
- **Generate** button produces four comma-separated lines, formatted to 3
  decimals to match the sequencer table display:
  - Co run: `OptoSigma:` line and `Thorlabs:` line
  - Cross run: `OptoSigma:` line and `Thorlabs:` line
- A copy-to-clipboard button per line (they paste directly into the
  Manual-mode angle boxes of `StageSequenceEditor`), plus **Save .txt**
  which writes all four lists with a header (records used, timestamps, φ
  grid) to a user-chosen path.

## Data / persistence

`hwp_calibration.json` (repo root, same pattern as `grating_calib.json`):

```json
{
  "incoming": {
    "stage": "optosigma",
    "peak_angle_deg": 2.13,
    "r2": 0.9991,
    "visibility": 0.98,
    "amp2_over_amp4": 0.012,
    "csv_path": "hwp_calib_incoming_20260716_143201.csv",
    "timestamp": "2026-07-16T14:32:01"
  },
  "outgoing": { ... same shape, "stage": "thorlabs" ... }
}
```

- Written atomically after every successful fit (scan or Load CSV).
- Loader tolerates a missing file or missing role keys (partial state is
  normal — that's the point of persisting).
- Raw scan CSVs are still written exactly like `waveplate_scan.py`
  (`hwp_calib_<role>_<timestamp>.csv`, `angle_deg,power_W`), so a record
  can always be re-derived from its CSV.

## Structure

- **Pure logic (module-level functions, no Qt):**
  - `compute_hwp_sequence(z_in, z_out, phis) -> {"co": (opto, tl), "cross": (opto, tl)}`
  - `phi_values(start, stop, step) -> list[float]` (inclusive stop, same
    ±1e-9 tolerance idiom as `StageSequenceEditor.angles`)
  - `load_records(path)` / `save_record(path, role, record)`
  - `format_angle_line(angles) -> str` (comma-separated, 3 decimals)
- **`ScanWorker(QThread)`:** owns stage + power meter for the duration of
  one scan. Connects on start, emits `point(angle, power)` per reading,
  `finished_ok(angles, powers)` / `failed(message)`, always disconnects in
  a `finally`. Reuses `find_pm100`, `read_power` and the two controller
  classes; `fit_waveplate` runs on the GUI side after the worker finishes.
- **`HwpPolarizationWindow(QWidget)`:** the three panels above; `main()`
  with `QApplication` so `uv run python hwp_polarization.py` launches it.

## Error handling

- Stage/PM connection failures surface as a message box with the underlying
  error (e.g. port held by `horibagui.py` — serial ports can't be shared;
  the message says to disconnect the stage there first).
- Fit failures / too-few-points: message box, record not written.
- Malformed CSV on Load: message box naming the expected columns.
- JSON corruption: warn and start with empty records (grating_calib
  pattern), never crash on load.

## Testing (`tests/test_hwp_polarization.py`, mock suite — no hardware, no qtbot)

- `compute_hwp_sequence`: co/cross offsets correct, 45° cross shift,
  φ/2 factor, list lengths match phis.
- `phi_values`: inclusive endpoints, non-divisible steps, zero/negative
  step returns empty.
- Record round-trip: save then load preserves both roles; partial file
  (one role) loads; corrupt file yields empty records.
- `format_angle_line` output pastes into the same parser
  `StageSequenceEditor.angles` uses (comma-split floats) — asserted by
  parsing the formatted string back.

## Out of scope

- No changes to `waveplate_scan.py` or `horibagui.py`.
- No interleaved co/cross sequences, no QWP math, no automatic pasting
  into the sequencer.
