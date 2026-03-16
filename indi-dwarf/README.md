# DWARF INDI Driver

**Author:** [@cgobat](https://github.com/cgobat)

## Overview

This subdirectory contains an INDI driver for DWARF smart telescopes.

The driver exposes DWARF functionality through the INDI protocol so it can be
used by INDI clients such as KStars/Ekos and other software that connects to
`indiserver`.

At a high level, the driver is responsible for:

- connecting to the DWARF device
- exposing telescope, focuser, filter, and camera-related INDI properties
- starting exposures and reading out image frames
- publishing image data through the `CCD1` BLOB vector
- translating INDI client actions into DWARF device operations

## Files

- `indi_dwarf.py`  
  Main pyINDI device implementation.

- `connection.py`  
  DWARF transport/session wrapper used by the device layer.

- `indi_device.py`  
  Shared device helpers and local abstractions used by the driver.

- `install_indi_drivers.py`  
  Helper script for installing the driver and associated metadata into standard
  INDI locations.

- `indi_dwarf_sk.xml`  
  INDI driver metadata used by INDI tooling.

- `config.ini`  
  Optional configuration defaults for the driver.

## Architecture

The driver is split into two main layers.

### Connection layer

The connection layer talks directly to the DWARF client/session code. Its job is to:

- establish and tear down the device session
- issue low-level commands
- query device state
- start an exposure
- read back image frames/data

### Device layer

The device layer is the pyINDI-facing part of the driver. Its job is to:

- define INDI properties and vectors
- handle `ISNew*` callbacks from clients
- manage INDI property state transitions
- encode images for publication
- publish image data on `CCD1`
- populate FITS headers from current camera/device state

This keeps INDI-specific behavior separate from transport/protocol details.

## Exposure and Image Flow

The intended exposure pipeline is:

1. INDI client requests an exposure through the `CCD_EXPOSURE` vector
2. device layer calls `start_exposure(duration)`
3. connection layer triggers the capture on the DWARF device
4. device layer waits for `read_exposure(...)`
5. connection layer returns the raw image frame as a NumPy array
6. device layer constructs a FITS HDU using the data and populates the header with metadata
7. device layer publishes the result through the `CCD1` BLOB vector

## Installation

A typical install flow is:

```bash
python -m pip install -U pyindi numpy astropy protobuf
python install_indi_drivers.py
```

Ensure that Python can import `dwarf_alpaca` in the same environment used to launch the driver.

## Running with indiserver

A typical launch command is:

```bash
indiserver -v indi_dwarf
```

This only works if:

- the installed `indi_dwarf` executable is on `PATH`
- it invokes the correct Python interpreter
- that interpreter has all required packages installed

## Source of Truth

Protocol behavior and hardware semantics should come from the DWARF
client/session implementation and protobuf definitions, not from duplicated
assumptions in the INDI layer.

The INDI-specific code in this subdirectory should focus on:

- property handling
- state mapping
- image publication
- interoperability with INDI clients
