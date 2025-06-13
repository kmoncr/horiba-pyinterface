# horiba-pyinterface

A Python interface for controlling the Horiba iHR 550 spectrometer and Synapse Plus CCD camera using the EZSpec SDK. Designed for use in research settings requiring programmable acquisition of spectra with user-defined parameters.

This project builds on [PyMeasure](https://pymeasure.readthedocs.io/en/latest/) for the instrument interface and GUI, and uses procedural control logic from [Horiba’s official Python SDK](https://github.com/HORIBAEzSpecSDK/python-sdk).

## Features

- Fully scriptable acquisition via the EZSpec SDK
- PyMeasure-managed graphical interface for scan configuration and execution
- Support for the following scan parameters:
  - Excitation wavelength
  - Center wavelength
  - Exposure time
  - Grating
  - Slit position
  - Gain
  - Scan speed
  - Number of scans
  - Filename/output path
- Results can be plotted in either **wavelength** or **wavenumber**

## Usage

Launch the GUI with:

```bash
python horibagui.py
```

Scan parameters can be input directly through the graphical interface.

## Environment Setup

This project uses [uv](https://github.com/astral-sh/uv) for reproducible environments.

```bash
uv venv
uv install
```

Ensure that the Horiba EZSpec SDK is installed and registered on your system; specifically, ICL.exe must be licensed and activated. 
