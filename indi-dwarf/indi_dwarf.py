#!/usr/bin/env python

import asyncio
import logging
import sys
import threading
import time
from collections import defaultdict
from io import BytesIO
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.time import Time, TimeDelta
from pyindi.device import (
    IBLOB,
    IBLOBVector,
    INumber,
    INumberVector,
    IPState,
    IPerm,
    ISRule,
    ISState,
    ISwitch,
    ISwitchVector,
    IText,
    ITextVector,
)

THIS_FILE_PATH = Path(__file__)
SRC_DIR = THIS_FILE_PATH.resolve().parent
sys.path.append(SRC_DIR.as_posix())
REPO_SRC = (SRC_DIR.parent / "src").resolve()
if REPO_SRC.is_dir():
    sys.path.append(REPO_SRC.as_posix())
else:
    raise FileNotFoundError(f"Expected to find directory '{REPO_SRC}' but didn't!")

from indi_device import INDIDevice
from install_indi_drivers import DRIVER_EXE_NAME, __version__
from connection import DEFAULT_ADDR, LOG_DIR, CONTROL_PORT, DwarfConnectionManager

logger = logging.getLogger(THIS_FILE_PATH.stem)
connection_managers = defaultdict(dict)


def get_connection_manager(address: str, port: int):
    global connection_managers
    cm = connection_managers[address].get(port)
    if cm is None:
        cm = connection_managers[address][port] = DwarfConnectionManager(address, port)
    return cm


class DwarfDevice(INDIDevice):
    def __init__(self, device_name: str = "DWARF", config=None, loop=None):
        super().__init__(loop=loop, config=config, name=device_name)
        self.connection: DwarfConnectionManager | None = None
        self.filter_names = ["VIS", "Astro", "Duo-band"]
        self._exposure_thread: threading.Thread | None = None
        self._exposure_started_at: Time | None = None
        self._last_exposure_duration: float | None = None

    @property
    def connected(self) -> bool:
        return bool(self.connection and self.connection.connected)

    def ISGetProperties(self, device=None):
        self.IDDef(
            ISwitchVector(
                [ISwitch("CONNECT", ISState.OFF, "Connect"), ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                self.name(), "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY, IPerm.RW, label="Connection", group="General"
            )
        )
        self.IDDef(
            ITextVector(
                [IText("IP_ADDRESS", DEFAULT_ADDR, "Address")],
                self.name(), "NETWORK_CONFIG", IPState.IDLE, IPerm.RW, label="Network", group="General"
            )
        )
        self.IDDef(
            ITextVector(
                [
                    IText("DRIVER_NAME", "pyINDI DWARF", "Driver name"),
                    IText("DRIVER_EXEC", DRIVER_EXE_NAME, "Driver exe"),
                    IText("DRIVER_VERSION", __version__, "Version"),
                    IText("DRIVER_INTERFACE", str(27), "Interface(s)"),
                ],
                self.name(), "DRIVER_INFO", IPState.IDLE, IPerm.RO, label="Driver Info", group="General"
            )
        )
        self.buildSkeleton(SRC_DIR / "indi_dwarf_sk.xml")
        self.IDDef(
            IBLOBVector([IBLOB("CCD1", format=".fits", label="Image data")], self.name(), "CCD1", IPState.IDLE, IPerm.RO, label="BLOB Data", group="Data")
        )

    def ISNewNumber(self, device, name, values, names):
        if name in ("EQUATORIAL_EOD_COORD", "TARGET_EOD_COORD"):
            ra = dec = None
            for propname, value in zip(names, values):
                if propname == "RA":
                    ra = float(value)
                elif propname == "DEC":
                    dec = float(value)
            if ra is None or dec is None:
                self.IDMessage("Missing RA/DEC values", msgtype="ERROR")
                return
            try:
                self.connection.slew_to_coordinates(ra, dec, target_name="INDI Target")
            except Exception as exc:
                self.IDMessage(f"DWARF slew failed: {exc}", msgtype="ERROR")
            else:
                self.IUUpdate(device, "EQUATORIAL_EOD_COORD", [ra, dec], ["RA", "DEC"], Set=True)
                if name.startswith("TARGET"):
                    self.IUUpdate(device, name, [ra, dec], ["RA", "DEC"], Set=True)
            return

        if name == "CCD_EXPOSURE":
            duration = float(values[0])
            try:
                self.start_exposure(duration)
            except Exception as exc:
                self.IDMessage(f"Exposure failed to start: {exc}", msgtype="ERROR")
            return

        if name == "CCD_BINNING":
            if len(set(int(v) for v in values)) != 1:
                self.IDMessage("Binning must be square (1x1 or 2x2)", msgtype="ERROR")
                return
            self.connection.set_binning(int(values[0]))
            self.IUUpdate(device, name, values, names, Set=True)
            return

        if name == "ABS_FOCUS_POSITION":
            target = int(values[0])
            code = self.connection.move_focuser_absolute(target)
            vec = self.IUUpdate(device, name, [target], names)
            vec.state = IPState.OK if code == 0 else IPState.ALERT
            self.IDSet(vec)
            return

        if name == "FILTER_SLOT":
            target = int(values[0])
            self.connection.set_filter_position(target)
            filter_name = self.filter_names[target] if 0 <= target < len(self.filter_names) else f"Filter {target}"
            self.IUUpdate(device, "FILTER_SLOT", [target], ["FILTER_SLOT_VALUE"], Set=True)
            self.IUUpdate(device, "FILTER_NAME", [filter_name], ["FILTER_NAME_VALUE"], Set=True)
            return

        self.IUUpdate(device, name, values, names, Set=True)

    def ISNewSwitch(self, device, name, values, names):
        if name == "CONNECTION":
            self.handle_connection_update(names, values)
            return

        if name.startswith("TELESCOPE_MOTION_"):
            motion_direction = [switch_name for switch_name, switch_state in zip(names, values) if switch_state == ISState.ON]
            if motion_direction:
                direction = motion_direction.pop().split("_")[-1].lower()
                self.connection.move_in_direction(direction, 0.5)
                time.sleep(0.5)
                self.IUUpdate(self.name(), name, [ISState.OFF] * len(values), names, Set=True)
            return

        if name == "TELESCOPE_ABORT_MOTION":
            keyvals = dict(zip(names, values))
            if keyvals.get("ABORT_MOTION") == ISState.ON:
                self.connection.abort_slew()
            return

        self.IUUpdate(device, name, values, names, Set=True)

    def ISNewText(self, device, name, values, names):
        if name == "TIME_UTC" and self.connected:
            try:
                self.connection.sync_clock()
            except Exception as exc:
                self.IDMessage(f"Clock sync failed: {exc}", msgtype="ERROR")
                return
        self.IUUpdate(device, name, values, names, Set=True)

    def start_exposure(self, duration: float) -> None:
        if not self.connected:
            raise RuntimeError("Device is not connected")
        if self._exposure_thread is not None and self._exposure_thread.is_alive():
            raise RuntimeError("An exposure is already in progress")

        duration = float(duration)
        self._exposure_started_at = Time.now()
        self._last_exposure_duration = duration

        exposure_vec = self.IUFind("CCD_EXPOSURE")
        exposure_vec.state = IPState.BUSY
        self.IDSet(exposure_vec)

        ccd1_vec = self.IUFind("CCD1")
        ccd1_vec.state = IPState.BUSY
        self.IDSet(ccd1_vec)

        self.connection.start_exposure(duration)
        self._exposure_thread = threading.Thread(
            target=self._complete_exposure,
            args=(duration,),
            daemon=True,
        )
        self._exposure_thread.start()

    def _complete_exposure(self, duration: float) -> None:
        try:
            data = self.connection.read_exposure(timeout=max(30.0, duration + 30.0))
            self.publish_image(data)
            exposure_vec = self.IUFind("CCD_EXPOSURE")
            exposure_vec.state = IPState.OK
            self.IDSet(exposure_vec)
        except Exception as exc:
            exposure_vec = self.IUFind("CCD_EXPOSURE")
            exposure_vec.state = IPState.ALERT
            self.IDSet(exposure_vec)
            ccd1_vec = self.IUFind("CCD1")
            ccd1_vec.state = IPState.ALERT
            self.IDSet(ccd1_vec)
            self.IDMessage(f"Exposure failed: {exc}", msgtype="ERROR")

    def _populate_fits_header(self, header: fits.Header) -> None:
        snapshot = self.connection.get_state_snapshot() if self.connection is not None else {}
        camera_state = dict(snapshot.get("camera_state") or {})
        focuser_state = dict(snapshot.get("focuser_state") or {})
        telescope_state = dict(snapshot.get("telescope_state") or {})

        start_time = self._exposure_started_at or Time.now()
        duration = float(self._last_exposure_duration or 0.0)
        end_time = start_time + TimeDelta(duration, format="sec")

        header.set("TELESCOP", snapshot.get("device_model", "DWARF"), "Telescope model")
        header.set("ORIGIN", "indi_dwarf", "Data acquisition software")
        header.set("DATE-OBS", start_time.utc.isot, "Exposure start time UTC")
        header.set("DATE-END", end_time.utc.isot, "Exposure end time UTC")
        header.set("EXPTIME", duration, "[s] Exposure time")
        header.set("OBJECT", telescope_state.get("target_name"), "Target name")
        header.set("RA", snapshot.get("ra_hours"), "Commanded right ascension [hours]")
        header.set("DEC", snapshot.get("dec_degs"), "Commanded declination [deg]")
        header.set("POCUSPOS", snapshot.get("focus_position", focuser_state.get("position")), "Focuser absolute position")
        # header.set("FILTER", snapshot.get("filter_name"), "Selected filter")
        header.set("FILTNUM", camera_state.get("filter_index"), "Active filter index")
        header.set("CCD-TEMP", snapshot.get("temperature_c", camera_state.get("temperature_c")), "[degC] Sensor temperature [C]")
        header.set("GAIN", snapshot.get("gain", camera_state.get("gain")), "Camera gain")
        header.set("OFFSET", snapshot.get("offset", camera_state.get("offset")), "Camera offset")
        header.set("SENSMODE", snapshot.get("sensor_mode", camera_state.get("sensor_mode")), "Sensor mode")
        header.set("XBINNING", (snapshot.get("binning") or camera_state.get("requested_bin") or [None, None])[0], "Binning factor in X")
        header.set("YBINNING", (snapshot.get("binning") or camera_state.get("requested_bin") or [None, None])[1], "Binning factor in Y")

        for key, fits_key, comment in (
            ("exposure_ms", "EXPT-MS", "Requested exposure [ms]"),
            ("exposure_us", "EXPT-US", "Requested exposure [us]"),
            ("destination", "DWARFADR", "DWARF control endpoint"),
        ):
            header.set(fits_key, snapshot.get(key) or camera_state.get(key), comment)

    def publish_image(self, img_data: np.ndarray) -> None:

        header = fits.Header()
        self._populate_fits_header(header)
        hdu = fits.PrimaryHDU(data=img_data, header=header)

        buffer = BytesIO()
        hdu.writeto(buffer, overwrite=True)
        buffer.seek(0)
        blob = buffer.read()

        ccd1_vec = self.IUFind("CCD1")
        ccd1_blob = ccd1_vec["CCD1"]
        ccd1_blob.value = blob
        ccd1_blob.format = ".fits"
        ccd1_blob.size = len(blob)

        self.IDSetBLOB(ccd1_blob)

    def handle_connection_update(self, actions, states):
        action = [act for act, switch in zip(actions, states) if switch == ISState.ON].pop()
        if action == "DISCONNECT":
            if self.connected:
                self.connection.disconnect()
            vector_state = IPState.IDLE
        elif action == "CONNECT":
            if self.connection is None:
                network_addr = self.IUFind("NETWORK_CONFIG")["IP_ADDRESS"].value
                self.connection = get_connection_manager(network_addr, CONTROL_PORT)
            self.connection.connect()
            vector_state = IPState.OK
            try:
                self.filter_names = self.connection.filter_names = self.connection.get_filter_labels()
            except Exception:
                logger.exception("Could not read filter labels")
            self.IUUpdate(self.name(), "FILTER_SLOT", [self.connection.get_filter_position()], ["FILTER_SLOT_VALUE"], Set=True)
            self.IUUpdate(self.name(), "FILTER_NAME", [self.filter_names[0]], ["FILTER_NAME_VALUE"], Set=True)
            self.IUUpdate(self.name(), "ABS_FOCUS_POSITION", [self.connection.get_focuser_position()], ["FOCUS_ABSOLUTE_POSITION"], Set=True)
            self.IUUpdate(self.name(), "FOCUS_MAX", [2000], ["FOCUS_MAX_VALUE"], Set=True)
            self.IUUpdate(self.name(), "CCD_TEMPERATURE", [self.connection.get_camera_temperature()], ["CCD_TEMPERATURE_VALUE"], Set=True)
        else:
            raise ValueError(f"Unrecognized connection action: {action}")

        connection_vec = self.IUUpdate(self.name(), "CONNECTION", states, actions)
        connection_vec.state = vector_state
        self.IDSet(connection_vec)


async def main() -> None:
    device = DwarfDevice()
    try:
        await device.astart()
    finally:
        try:
            if device.connection is not None and device.connected:
                device.connection.disconnect()
        except Exception:
            logger.exception("Error while disconnecting DWARF during shutdown")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
