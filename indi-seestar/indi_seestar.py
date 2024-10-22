#!/usr/bin/env python3

import sys
import time
import tzlocal
import logging
import datetime as dt
from pathlib import Path
from collections import defaultdict

from pyindi.device import (device as IDevice, INumberVector, ISwitchVector, ITextVector,
                           INumber, ISwitch, IText, IPerm, IPState, ISState, ISRule)

THIS_FILE_PATH = Path(__file__) # leave symlinks as-is/unresolved
sys.path.append(THIS_FILE_PATH.resolve().parent.as_posix()) # resolve source directory

from socket_connections import (DEFAULT_ADDR, CONFIG_DIR, CONTROL_PORT, IMAGING_PORT, LOGGING_PORT,
                                RPCConnectionManager, ImageConnectionManager, LogConnectionManager)


logger = logging.getLogger(THIS_FILE_PATH.stem)
connection_managers = defaultdict(dict)


def get_connection_manager(address: str, port: int, kind: str):
    global connection_managers
    cm = connection_managers[address].get(port)
    if cm is None:
        cls = {"rpc": RPCConnectionManager, "img": ImageConnectionManager,
               "log": LogConnectionManager}[kind.lower()]
        cm = connection_managers[address][port] = cls(address, port)
    return cm


class SeestarCommon(IDevice):

    def __init__(self, name=None, host=DEFAULT_ADDR):
        super().__init__(name=name)
        self.connection: RPCConnectionManager = get_connection_manager(host, CONTROL_PORT, "rpc")

    @property
    def connected(self) -> bool:
        return self.connection.connected

    def ISGetProperties(self, device=None):
        """Called when client or indiserver sends `getProperties`."""

        self.IDDef(ISwitchVector([ISwitch("CONNECT", ISState.OFF, "Connect"),
                                  ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                                 self._devname, "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="Connection"),
                   None)

        utc_now = dt.datetime.now().astimezone(dt.timezone.utc)
        self.IDDef(ITextVector([IText("UTC", utc_now.strftime("%Y-%m-%dT%H:%M:%S.%f")),
                                IText("OFFSET", "+0000")],
                               self._devname, "TIME_UTC", IPState.IDLE, IPerm.RW, timeout=1),
                   None)

    def ISNewNumber(self, device, name, values, names):
        """A numeric vector has been updated from the client."""

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        vec = self.IUUpdate(device, name, values, names, Set=True)

    def ISNewSwitch(self, device, name, values, names):
        """A switch vector has been updated from the client."""

        if name == "CONNECTION":
            self.handle_connection_update(device, names, values)
        else:
            self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
            vec = self.IUUpdate(device, name, values, names, Set=True)

    def ISNewText(self, device, name, values, names):
        """A text vector has been updated from the client."""

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        self.IUUpdate(device, name, values, names, Set=True)

    def handle_connection_update(self, devname, actions: "list[str]", states: "list[ISState]"):
        conn = self.IUUpdate(devname, "CONNECTION", states, actions)
        if conn["DISCONNECT"].value == ISState.ON:
            self.connection.disconnect() # also stops listening and heartbeat
            conn.state = IPState.IDLE
        elif conn["CONNECT"].value == ISState.ON:
            self.connection.connect() # automatically starts heartbeat
            self.connection.start_listening()
            conn.state = IPState.OK
            self.on_connect()

        self.IDSet(conn)

    def on_connect(self):
        ...


class SeestarScope(SeestarCommon):

    def __init__(self, name="Seestar S50 Telescope", host=DEFAULT_ADDR):
        super().__init__(name, host)

    def ISGetProperties(self, device=None):

        self.IDDef(INumberVector([INumber("RA", "%2.8f", 0, 24, 1, 0.0, label="RA"),
                                  INumber("DEC", "%2.8f", -90, 90, 1, 0.0, label="Dec")],
                                 self._devname, "EQUATORIAL_EOD_COORD", IPState.OK, IPerm.RW,
                                 label="Pointing Coordinates"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("SLEW", ISState.ON, "Slew"),
                                  ISwitch("TRACK", ISState.OFF, "Track"),
                                  ISwitch("SYNC", ISState.OFF, "Sync")],
                                 self._devname, "ON_COORD_SET", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="On coord set"),
                   None)
        
        optics = {"focal_len": 250.0, "fnumber": 5.0}
        self.IDDef(INumberVector([INumber("TELESCOPE_APERTURE", format="%f", min=0, max=10000, step=1,
                                          value=optics["focal_len"]/optics["fnumber"], label="Aperture (mm)"),
                                  INumber("TELESCOPE_FOCAL_LENGTH", format="%f", min=0, max=100000, step=1,
                                          value=optics["focal_len"], label="Focal Length (mm)")],
                                 self._devname, "TELESCOPE_INFO", IPState.IDLE, IPerm.RO, label="Optical Properties"),
                   None)

        self.IDDef(INumberVector([INumber("DEW_HEATER_POWER", format="%f", min=0, max=100, step=1,
                                          value=0, label="Power Setting (%)")],
                                 self._devname, "DEW_HEATER", state=IPState.IDLE, perm=IPerm.RW,
                                 label="Dew Heater Power"),
                   None)
        
        self.IDDef(ISwitchVector([ISwitch("PARK", ISState.ON, "Close/Lower Arm"),
                                  ISwitch("UNPARK", ISState.OFF, "Raise Arm")],
                                 self._devname, "TELESCOPE_PARK", state=IPState.IDLE, rule=ISRule.ONEOFMANY,
                                 perm=IPerm.RW, label="Raise/Lower Arm"),
                   None)
        
        self.IDDef(ISwitchVector([ISwitch("PIER_EAST", ISState.OFF),
                                  ISwitch("PIER_WEST", ISState.OFF)],
                                 self._devname, "TELESCOPE_PIER_SIDE", state=IPState.IDLE,
                                 rule=ISRule.ATMOST1, perm=IPerm.RO, label="Mount Pier Side"),
                   None)

    def ISNewNumber(self, device, name, values, names):
        """A number vector has been updated from the client."""

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        
        if name == "EQUATORIAL_EOD_COORD":
            current = self["EQUATORIAL_EOD_COORD"]
            ra, dec = float(current['RA'].value), float(current['DEC'].value)

            self.IDMessage(f"Current pointing: RA={ra}, Dec={dec}")

            for name, value in zip(names, values):
                if name == 'RA':
                    ra = value
                elif name == 'DEC':
                    dec = value

            self.IDMessage(f"Requested RA/Dec: ({ra}, {dec})")

            switch = self['ON_COORD_SET']
            if switch['SLEW'].value == ISState.ON or switch['TRACK'].value == ISState.ON:
                # Slew/GoTo requested
                if self.is_moving():
                    self.connection.rpc_command("scope_abort_slew")
                cmd = "iscope_start_view"
                params = {"mode": "star", "target_ra_dec": [ra, dec], "target_name": "Stellarium Target", "lp_filter": False}
            elif switch["SYNC"].value == ISState.ON:
                # Sync requested
                cmd = "scope_sync"
                params = [ra, dec]

            try:
                response = self.connection.send_cmd_and_await_response(cmd, params=params)
                self.IUUpdate(device, name, [ra, dec], # TODO: read this from response
                              ["RA", "DEC"], Set=True)

            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}", msgtype="ERROR")

        elif name == "DEW_HEATER":
                heater = self.IUUpdate(device, name, values, names)
                power = values[0]
                reply = self.connection.send_cmd_and_await_response("pi_output_set2",
                                                                    params={"heater": {"state" :power>0,
                                                                                       "value": power}})
                if reply["code"]:
                    heater.state = IPState.ALERT
                    self.IDMessage("Error setting dew heater power", msgtype="ERROR")
                else:
                    heater.state = IPState.OK
                self.IDSet(heater)

    def ISNewSwitch(self, device, name, values, names):
        """A switch vector has been updated from the client."""

        try:
            self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")

            if name == "CONNECTION":
                self.handle_connection_update(device, names, values)

            elif name == "TELESCOPE_ABORT_MOTION":
                keyvals = dict(zip(names, values))
                if keyvals["ABORT_MOTION"] == ISState.ON:
                    self.connection.rpc_command("scope_abort_slew")

            elif name == "TELESCOPE_PARK":
                for name, value in zip(names, values):
                    if value == ISState.ON:
                        if name == "UNPARK":
                            self.unpark()
                        elif name == "PARK":
                            self.park()

            else:
                prop = self.IUUpdate(device, name, values, names)
                self.IDSet(prop)

        except Exception as error:
            self.IDMessage(f"Error updating {name} property: {error}")
            raise
        
    @IDevice.repeat(2000) # ms
    def do_repeat(self):
        """Tasks to repeat every other second."""

        if not self.connected:
            return

        self.IDMessage("Running telescope loop")

        try:
            result = self.connection.send_cmd_and_await_response("scope_get_equ_coord")["result"]
            ra = result['ra']
            dec = result['dec']
            self.IUUpdate(self._devname, "EQUATORIAL_EOD_COORD", [ra, dec], ["RA", "DEC"], Set=True)
            
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")

    def is_moving(self):
        """Checks if the mount is currently moving and returns True if it is."""

        result = self.connection.send_cmd_and_await_response("get_device_state",
                                                             params={"keys": ["mount"]})["result"]["mount"]
        return result["move_type"] != "none"

    def park(self):
        self.connection.rpc_command("scope_park")
        self.IUUpdate(self._devname, "TELESCOPE_PARK", [ISState.ON, ISState.OFF], ["PARK", "UNPARK"], Set=True)

    def unpark(self):
        if self["TELESCOPE_PARK"]["PARK"].value == ISState.ON: # if currently parked
            self.connection.rpc_command("scope_move_to_horizon")
        else:
            logger.info("Seestar is already unparked.")
        self.IUUpdate(self._devname, "TELESCOPE_PARK", [ISState.OFF, ISState.ON], ["PARK", "UNPARK"], Set=True)

    def rotate_cw_by_angle(self, angle: int):
        """Rotate the telescope by `angle` degrees clockwise around the azimuth axis.
        Use negative values to turn counter-clockwise."""
        self.connection.rpc_command("scope_move_left_by_angle", params=[angle]) # even though it says left, positive numbers move it right (clockwise)


class SeestarCamera(SeestarCommon):
    
    def __init__(self, name="Seestar S50 Camera", host=DEFAULT_ADDR):
        super().__init__(name=name, host=host)
        self.image_connection: ImageConnectionManager = get_connection_manager(host, IMAGING_PORT, "img")

    def ISGetProperties(self, device=None):
        super().ISGetProperties(device)

        control_defs = self.connection.send_cmd_and_await_response("get_controls")["result"]
        cam_controls = []
        for control in control_defs:
            if control["name"].startswith("ISP_"):
                continue # skip ISP controls
            response = self.connection.send_cmd_and_await_response("get_control_value", params=[control["name"]])
            try:
                current_value = response["result"]["value"]
            except KeyError:
                logger.exception(f"{control['name']}: {response}")
            if control["name"] == "Temperature":
                temperature = INumber("CCD_TEMPERATURE_VALUE", format="%f", min=-273.15, max=100., step=0.1,
                                      value=current_value, label="Temperature (degC)")
                continue
            elif control["read_only"]:
                logger.warning(f"Read-only camera property: {control['name']}")
                continue
            number = INumber("CCD_"+control["name"].upper(), "%f", control["min"], control["max"],
                             1, current_value, label=control["name"])
            cam_controls.append(number)

        self.IDDef(INumberVector([temperature], self._devname, "CCD_TEMPERATURE", IPState.IDLE, IPerm.RO, label="Camera Temperature"),
                   None)

        self.IDDef(INumberVector(cam_controls, self._devname, "CCD_CONTROLS", IPState.OK, IPerm.RW, label="Camera Controls"),
                   None)

        cam_info = self.connection.send_cmd_and_await_response("get_camera_info")["result"]

        self.IDDef(INumberVector([INumber("CCD_MAX_X", format="%d", min=0, max=None, step=1, value=cam_info["chip_size"][0]),
                                  INumber("CCD_MAX_Y", format="%d", min=0, max=None, step=1, value=cam_info["chip_size"][1]),
                                  INumber("CCD_PIXEL_SIZE", format="%f", min=0, max=None, step=1, value=cam_info["pixel_size_um"]),
                                  INumber("CCD_PIXEL_SIZE_X", format="%f", min=0, max=None, step=1, value=cam_info["pixel_size_um"]),
                                  INumber("CCD_PIXEL_SIZE_Y", format="%f", min=0, max=None, step=1, value=cam_info["pixel_size_um"]),
                                  INumber("CCD_BITSPERPIXEL", format="%d", min=0, max=32, step=4, value=16)],
                                 self._devname, "CCD_INFO", IPState.IDLE, IPerm.RO, label="Camera Properties"),
                   None)

        self.IDDef(ITextVector([IText("CFA_OFFSET_X", "0", "Bayer pattern X offset"),
                                IText("CFA_OFFSET_Y", "0", "Bayer pattern Y offset"),
                                IText("CFA_TYPE", "GRBG", "Bayer pattern order")], # TODO: read from 'debayer_pattern' instead of hardcoding
                               self._devname, "CCD_CFA", IPState.IDLE, IPerm.RO, label="Bayer Pattern"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("CONNECT", ISState.OFF, "Connect", ),
                                  ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                                 self._devname, "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="Connection"),
                   None)

    def ISNewText(self, device, name, values, names):
        """
        A text vector has been updated from 
        the client. 
        """
        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        self.IUUpdate(device, name, values, names, Set=True)

    def ISNewNumber(self, device, name, values, names):
        """
        A number vector has been updated from the client.
        """
        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        
        if name == "CCD_EXPOSURE":
            self.IDMessage(f"Initiating {values[0]} sec exposure")
            
            try:
                self.connection.rpc_command("start_exposure", params={})
                time.sleep(values[0])
                self.connection.rpc_command("stop_exposure")
                
            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}")

    def ISNewSwitch(self, device, name, values, names):
        """
        A switch has been updated from the client.
        """

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")

        try:
            if name == "CONNECTION":
                self.handle_connection_update(device, names, values)
            else:
                prop = self.IUUpdate(device, name, values, names, Set=True)

        except Exception as error:
            self.IDMessage(f"Error updating {name} property: {error}")
    
    @IDevice.repeat(2000)
    def do_repeat(self):
        
        self.IDMessage("Running camera loop")

        try:
            result = self.connection.send_cmd_and_await_response("get_control_value", params=["Temperature"])["result"]
            self.IUUpdate(self._devname, 'CCD_TEMPERATURE', [result["value"]], ["CCD_TEMPERATURE_VALUE"], Set=True)
            
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")


class SeestarFocuser(SeestarCommon):
    ...


class SeestarFilter(SeestarCommon):
    ...


if __name__ == "__main__":

    # while not scope_connection.event_list:
    #     time.sleep(0.01)
    # initial_event = scope_connection.event_list[0]
    # t0 = float(initial_event["Timestamp"])
    # logger.debug(f"Initial event recorded at t={t0}")
    now = time.localtime()

    if THIS_FILE_PATH.name == "indi_seestar_scope":
        scope = SeestarScope()
        scope.start()
    elif THIS_FILE_PATH.name == "indi_seestar_ccd":
        camera = SeestarCamera()
        camera.start()
    elif THIS_FILE_PATH.name == "indi_seestar_focuser":
        focuser = SeestarFocuser()
        focuser.start()
    elif THIS_FILE_PATH.name == "indi_seestar_filterwheel":
        filter_wheel = SeestarFilter()
        filter_wheel.start()
    else:
        scope_connection: RPCConnectionManager = get_connection_manager(DEFAULT_ADDR, CONTROL_PORT, "rpc")
        scope_connection.start_listening()
        camera_connection: ImageConnectionManager = get_connection_manager(DEFAULT_ADDR, IMAGING_PORT, "img")
        camera_connection.start_listening()

        time.sleep(1.0)

        if "--set-time" in sys.argv:
            logger.info(f"Setting Seestar time to {now}")
            scope_connection.rpc_command("pi_set_time",
                                         params={"year": now.tm_year, "mon": now.tm_mon, "day": now.tm_mday,
                                                 "hour": now.tm_hour, "min": now.tm_min, "sec": now.tm_sec,
                                                 "time_zone": tzlocal.get_localzone_name()})
            time.sleep(0.5)
        if "--get-log" in sys.argv:
            log_connection: LogConnectionManager = get_connection_manager(DEFAULT_ADDR, LOGGING_PORT, "log")
            zip_data = log_connection.get_log_dump()
            with (CONFIG_DIR/"seestar_svr_log.zip").open("wb+") as zipfile:
                zipfile.write(zip_data)
            log_connection.disconnect()
            time.sleep(0.5)
        # scope_connection.rpc_command("get_view_state")
        # time.sleep(0.5)
        scope_connection.rpc_command("get_device_state", params={"keys": ["device", "camera", "pi_status"]})
        time.sleep(0.5)
        # scope_connection.rpc_command("scope_get_ra_dec")
        # time.sleep(0.5)
        # scope_connection.rpc_command("get_camera_state")
        # time.sleep(0.5)
        scope_connection.rpc_command("get_wheel_state")
        time.sleep(0.5)
        scope_connection.rpc_command("get_camera_info")
        time.sleep(0.5)
        scope_connection.rpc_command("pi_get_info")
        # time.sleep(0.5)
        # camera_connection.rpc_command("get_rtmp_config")
        # time.sleep(0.5)
        # scope_connection.rpc_command("scope_is_moving")
        time.sleep(0.5)
        scope_connection.rpc_command("get_setting")
        # time.sleep(0.5)
        # scope_connection.rpc_command("get_camera_exp_and_bin")
        # time.sleep(0.5)
        # scope_connection.rpc_command("get_control_value", params=["Exposure"])

    # time.sleep(0.2)
    # print(cam["CCD_TEMPERATURE"].elements)
    # time.sleep(0.2)
    # print(cam["CCD_TEMPERATURE"].elements)
    # print(cam["CCD_CONTROLS"].elements)
