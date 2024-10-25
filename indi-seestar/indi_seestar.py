#!/usr/bin/env python3

import sys
import time
import logging
import tzlocal
import datetime as dt
from pathlib import Path
from collections import defaultdict

from pyindi.device import (INumberVector, ISwitchVector, ITextVector, IBLOBVector,
                           INumber, ISwitch, IText, IBLOB, IPerm, ISRule, IPState, ISState)

THIS_FILE_PATH = Path(__file__) # leave symlinks as-is/unresolved
sys.path.append(THIS_FILE_PATH.resolve().parent.as_posix()) # resolve source directory

from indi_device import MultiDevice
from socket_connections import (DEFAULT_ADDR, CONFIG_DIR, CONTROL_PORT, IMAGING_PORT, LOGGING_PORT, GUIDER_PORT,
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


class SeestarDevice(MultiDevice):

    def __init__(self, host=DEFAULT_ADDR, scope_name = "Seestar S50 Telescope",
                 camera_name = "Seestar S50 Camera", focuser_name = "Seestar S50 Focuser",
                 filterwheel_name = "Seestar S50 Filter Wheel", config=None, loop=None):
        super().__init__([scope_name, camera_name, focuser_name, filterwheel_name],
                         config, loop)
        self.scope_device = scope_name
        self.camera_device = camera_name
        self.focuser_device = focuser_name
        self.filterwheel_device = filterwheel_name
        self.connection: RPCConnectionManager = get_connection_manager(host, CONTROL_PORT, "rpc")
        self.guide_connection: RPCConnectionManager = get_connection_manager(host, GUIDER_PORT, "rpc")
        self.imager_connection: ImageConnectionManager = get_connection_manager(host, IMAGING_PORT, "img")
        self.log_connection: LogConnectionManager = get_connection_manager(host, LOGGING_PORT, "log")

    @property
    def connected(self) -> bool:
        return self.connection.connected

    def ISGetProperties(self, device=None):
        """Called when client or indiserver sends `getProperties`."""

        for dev in self.device_names:
            self.IDDef(ISwitchVector([ISwitch("CONNECT", ISState.OFF, "Connect"),
                                      ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                                     dev, "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY,
                                     IPerm.RW, label="Device Connection"),
                       None)
            
        # **** telescope-specific properties ****

        self.IDDef(INumberVector([INumber("RA", "%2.8f", 0, 24, 1, 0.0, label="RA"),
                                  INumber("DEC", "%2.8f", -90, 90, 1, 0.0, label="Dec")],
                                 self.scope_device, "EQUATORIAL_EOD_COORD", IPState.OK, IPerm.RW,
                                 label="Sky Coordinates"),
                   None)

        self.IDDef(INumberVector([INumber("ALT", "%2.8f", -90, 90, 1, 0.0, label="Altitude"),
                                  INumber("AZ", "%2.8f", 0, 360, 1, 0.0, label="Azimuth")],
                                 self.scope_device, "HORIZONTAL_COORD", IPState.OK, IPerm.RW,
                                 label="Topocentric Coordinates"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("SLEW", ISState.ON, "Slew"),
                                  ISwitch("TRACK", ISState.OFF, "Track"),
                                  ISwitch("SYNC", ISState.OFF, "Sync")],
                                 self.scope_device, "ON_COORD_SET", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="On coord set"),
                   None)

        optics = {"focal_len": 250.0, "fnumber": 5.0}
        self.IDDef(INumberVector([INumber("TELESCOPE_APERTURE", format="%f", min=0, max=None, step=1,
                                          value=optics["focal_len"]/optics["fnumber"], label="Aperture (mm)"),
                                  INumber("TELESCOPE_FOCAL_LENGTH", format="%f", min=0, max=None, step=1,
                                          value=optics["focal_len"], label="Focal Length (mm)")],
                                 self.scope_device, "TELESCOPE_INFO", IPState.IDLE, IPerm.RO, label="Optical Properties"),
                   None)

        self.IDDef(INumberVector([INumber("DEW_HEATER_POWER", format="%f", min=0, max=100, step=1,
                                          value=0, label="Power Setting (%)")],
                                 self.scope_device, "DEW_HEATER", state=IPState.IDLE, perm=IPerm.RW,
                                 label="Dew Heater Power"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("PARK", ISState.ON, "Close/Lower Arm"),
                                  ISwitch("UNPARK", ISState.OFF, "Raise Arm")],
                                 self.scope_device, "TELESCOPE_PARK", state=IPState.IDLE, rule=ISRule.ONEOFMANY,
                                 perm=IPerm.RW, label="Raise/Lower Arm"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("PIER_EAST", ISState.OFF),
                                  ISwitch("PIER_WEST", ISState.OFF)],
                                 self.scope_device, "TELESCOPE_PIER_SIDE", state=IPState.IDLE,
                                 rule=ISRule.ATMOST1, perm=IPerm.RO, label="Mount Pier Side"),
                   None)

        # **** camera-specific properties ****

        self.IDDef(INumberVector([INumber("CCD_MAX_X", format="%d", min=0, max=None, step=1, value=1080),
                                  INumber("CCD_MAX_Y", format="%d", min=0, max=None, step=1, value=1920),
                                  INumber("CCD_PIXEL_SIZE", format="%f", min=0, max=None, step=1, value=2.9),
                                  INumber("CCD_PIXEL_SIZE_X", format="%f", min=0, max=None, step=1, value=2.9),
                                  INumber("CCD_PIXEL_SIZE_Y", format="%f", min=0, max=None, step=1, value=2.9),
                                  INumber("CCD_BITSPERPIXEL", format="%d", min=0, max=32, step=4, value=16)],
                                 self.camera_device, "CCD_INFO", IPState.IDLE, IPerm.RO, label="Camera Properties"),
                   None)

        self.IDDef(ITextVector([IText("CFA_OFFSET_X", "0", "Bayer X offset"),
                                IText("CFA_OFFSET_Y", "0", "Bayer Y offset"),
                                IText("CFA_TYPE", "GRBG", "Bayer pattern")],
                               self.camera_device, "CCD_CFA", IPState.IDLE, IPerm.RO, label="Bayer Matrix"),
                   None)
        
        self.IDDef(ISwitchVector([ISwitch("FRAME_LIGHT", ISState.ON, label="Light"),
                                  ISwitch("FRAME_BIAS", ISState.OFF, label="Bias"),
                                  ISwitch("FRAME_DARK", ISState.OFF, label="Dark"),
                                  ISwitch("FRAME_FLAT", ISState.OFF, label="Flat")],
                                 self.camera_device, "CCD_FRAME_TYPE", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="Exposure Type"),
                   None)
        
        self.IDDef(INumberVector([INumber("CCD_TEMPERATURE_VALUE", format="%f", min=-273.15, max=100., step=0.1,
                                          value=0.0, label="Temperature (C)")],
                                 self.camera_device, "CCD_TEMPERATURE", IPState.IDLE, IPerm.RO, label="Camera Temperature"),
                   None)

        
        # **** focuser-specific properties ****

        self.IDDef(INumberVector([INumber("FOCUS_ABSOLUTE_POSITION", "%d", 0, 2600,
                                          5, value=1700, label="Focuser Position")],
                                 self.focuser_device, "ABS_FOCUS_POSITION",
                                 state=IPState.OK, perm=IPerm.RW),
                   None)
        
        self.IDDef(INumberVector([INumber("FOCUS_MAX_VALUE", "%d", None, None,
                                          1, 2600, label="Focuser Maximum")], # TODO: is this the same for everyone?
                                 self.focuser_device, "FOCUS_MAX", state=IPState.IDLE, perm=IPerm.RO),
                   None)
        
        self.IDDef(ISwitchVector([ISwitch("START_AUTOFOCUS", ISState.OFF, "Start Autofocus Routine"),
                                  ISwitch("STOP_AUTOFOCUS", ISState.OFF, "Stop Autofocus Routine")],
                                 self.focuser_device, "AUTOFOCUS", state=IPState.IDLE, rule=ISRule.ATMOST1,
                                 perm=IPerm.WO, timeout=10., label="Autofocus"),
                   None)
        
        # **** filter wheel-specific properties ****

        self.IDDef(INumberVector([INumber("FILTER_SLOT_VALUE", format="%d", min=0, max=3, step=1,
                                          value=3, label="Current Filter Position")],
                                 self.filterwheel_device, "FILTER_SLOT", state=IPState.IDLE, perm=IPerm.RW),
                   None)

        self.IDDef(ITextVector([IText("FILTER_NAME_VALUE", "unset", label="Current Filter Name")],
                                 self.filterwheel_device, "FILTER_NAME", state=IPState.IDLE, perm=IPerm.RO,
                                 label="Active Filter"),
                   None)

    def ISNewNumber(self, device, name, values, names):
        """A numeric vector has been updated from the client."""

        if name in ("EQUATORIAL_EOD_COORD", "TARGET_EOD_COORD"):
            current = self["EQUATORIAL_EOD_COORD"]
            curr_ra, curr_dec = float(current['RA'].value), float(current['DEC'].value)

            self.IDMessage(f"Current pointing: ({curr_ra}, {curr_dec})", msgtype="DEBUG", dev=self.scope_device)

            for propname, value in zip(names, values):
                if propname == "RA":
                    ra = value
                elif propname == "DEC":
                    dec = value

            self.IDMessage(f"Requested ({ra=}, {dec=})", msgtype="DEBUG", dev=self.scope_device)

            switch = self['ON_COORD_SET']
            if switch['SLEW'].value == ISState.ON or switch['TRACK'].value == ISState.ON:
                # Slew/GoTo requested
                if self.is_moving():
                    self.connection.rpc_command("scope_abort_slew")
                cmd = "iscope_start_view"
                params = {"mode": "star", "target_ra_dec": [ra, dec], "target_name": "INDI Target", "lp_filter": False}
            elif switch["SYNC"].value == ISState.ON:
                # Sync requested
                cmd = "scope_sync"
                params = [ra, dec]

            try:
                response = self.connection.send_cmd_and_await_response(cmd, params=params)
                assert response["code"] == 0, f"Got non-zero response code {response['code']} ({response.get('error')})"

            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}", msgtype="ERROR", dev=self.scope_device)

            if name.startswith("TARGET"): # target coords don't automatically get updated in loop
                self.IUUpdate(device, name, values, names, Set=True)

        elif name == "DEW_HEATER":
                heater = self.IUUpdate(device, name, values, names)
                power = values[0]
                reply = self.connection.send_cmd_and_await_response("pi_output_set2",
                                                                    params={"heater": {"state": power>0,
                                                                                       "value": power}})
                if reply["code"]:
                    heater.state = IPState.ALERT
                    self.IDMessage("Error setting dew heater power", msgtype="ERROR", dev=self.scope_device)
                else:
                    heater.state = IPState.OK
                self.IDSet(heater)

        elif name == "CCD_EXPOSURE":
            self.IDMessage(f"Initiating {values[0]} sec exposure", msgtype="DEBUG", dev=self.camera_device)

            try:
                self.connection.rpc_command("set_control_value",
                                            params=["Exposure", int(values[0]*1000000)]) # in microseconds
                self.connection.rpc_command("start_exposure", params=["light", False])

            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}", msgtype="ERROR", dev=self.camera_device)

        elif name == "ABS_FOCUS_POSITION":
            assert names[0] == "FOCUS_ABSOLUTE_POSITION"
            code = self.move_focuser_absolute(values[0])
            if code:
                self.IDMessage(f"Attempt to set focuser position to {values[0]} returned code {code}.",
                               msgtype="ERROR", dev=self.focuser_device)
                state = IPState.ALERT
            else:
                state = IPState.OK
            vec = self.IUUpdate(device, name, [], names)
            vec.state = state

        elif name == "FILTER_SLOT":
            assert names == ["FILTER_SLOT_VALUE"]
            self.set_filter_position(values[0])

        else:
            self.IDMessage(f"Client sent update for {device}'s {name} vector: {dict(zip(names, values))}",
                           msgtype="DEBUG", dev=device)
            vec = self.IUUpdate(device, name, values, names, Set=True)

    def ISNewSwitch(self, device, name, values, names):
        """A switch vector has been updated from the client."""

        if name == "CONNECTION":
            self.handle_connection_update(names, values)

        elif name == "TELESCOPE_ABORT_MOTION":
            keyvals = dict(zip(names, values))
            if keyvals["ABORT_MOTION"] == ISState.ON:
                self.connection.rpc_command("scope_abort_slew")

        elif name == "TELESCOPE_PARK":
            for name, value in zip(names, values):
                if value == ISState.ON:
                    if name == "UNPARK":
                        self.unpark_mount()
                    elif name == "PARK":
                        self.park_mount()

        elif name == "AUTOFOCUS":
            action_name = [action for action, state in zip(names, values) if state==ISState.ON].pop()
            if action_name == "START_AUTOFOCUS":
                self.start_auto_focus()
                vec: ISwitchVector = self.IUUpdate(device, name, values, names)
                vec.state = IPState.BUSY
                self.IDSet(vec, msg="Autofocusing")
            elif action_name == "STOP_AUTOFOCUS":
                self.stop_auto_focus()
                vec = self.IUUpdate(device, name, values, names)
                vec.state = IPState.IDLE
                self.IDSet(vec, msg="Autofocus canceled")

        else:
            self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}", msgtype="DEBUG", dev=device)
            vec = self.IUUpdate(device, name, values, names, Set=True)

    def ISNewText(self, device, name, values, names):
        """A text vector has been updated from the client."""

        if name == "TIME_UTC":
            for prop, value in zip(names, values):
                if prop == "UTC":
                    time_set = dt.datetime.fromisoformat(value)
                elif prop == "OFFSET":
                    utc_offset = int(value.rstrip("0"))
                    sign = "-" if utc_offset<0 else "+"
            time_utc = (time_set - dt.timedelta(hours=utc_offset)).timetuple()
            self.IDMessage(f"Setting device time to {time.strftime('%Y-%m-%dT%H:%M:%S', time_utc)}",
                           msgtype="INFO", dev=self.scope_device)
            # self.connection.rpc_command("scope_set_time", params=[time_set.isoformat(), f"{sign}{abs(utc_offset)}"])
            self.connection.rpc_command("pi_set_time", params={"year": time_utc.tm_year, "mon": time_utc.tm_mon,
                                                               "day": time_utc.tm_mday, "hour": time_utc.tm_hour,
                                                               "min": time_utc.tm_min, "sec": time_utc.tm_sec,
                                                               "time_zone": "Etc/UTC"})
        else:
            self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}", msgtype="DEBUG", dev=device)
            self.IUUpdate(device, name, values, names, Set=True)

    def handle_connection_update(self, actions: "list[str]", states: "list[ISState]"):
        action = [act for act, switch in zip(actions, states) if switch==ISState.ON].pop()
        if action == "DISCONNECT":
            self.connection.disconnect() # also stops listening and heartbeat
            vector_state = IPState.IDLE

        elif action == "CONNECT":
            self.connection.connect() # automatically starts heartbeat
            self.connection.start_listening()
            vector_state = IPState.OK

            # actions to perform on connect

            self.define_camera_controls()

            filter_pos = self.get_filter_position()
            self.filter_names = self.connection.send_cmd_and_await_response("get_wheel_slot_name")["result"]
            self.IUUpdate(self.filterwheel_device, "FILTER_SLOT", [filter_pos], ["FILTER_SLOT_VALUE"], Set=True)
            self.IUUpdate(self.filterwheel_device, "FILTER_NAME", [self.filter_names[filter_pos]],
                          ["FILTER_NAME_VALUE"], Set=True)

            focus_state = self.connection.send_cmd_and_await_response("get_device_state",
                                                                      params={"keys": ["focuser"]})
            focuser = focus_state["result"]["focuser"]
            foc_max_vec: INumberVector = self.IUUpdate(self.focuser_device, "FOCUS_MAX", [focuser["max_step"]],
                                                       ["FOCUS_MAX_VALUE"])
            foc_max_num: INumber = foc_max_vec["FOCUS_MAX_VALUE"]
            foc_max_num.value = foc_max_num.min = foc_max_num.max = focuser["max_step"]
            self.IDSet(foc_max_vec)
            foc_abs_vec: INumberVector = self.IUUpdate(self.focuser_device, "ABS_FOCUS_POSITION", [focuser["step"]],
                                                       ["FOCUS_ABSOLUTE_POSITION"])
            foc_pos: INumber = foc_abs_vec["FOCUS_ABSOLUTE_POSITION"]
            foc_pos.max = focuser["max_step"]
            self.IDSet(foc_abs_vec)

        else:
            raise ValueError(f"Unrecognized connection action: {action}")

        for dev in self.device_names:
            connection_vec = self.IUUpdate(dev, "CONNECTION", states, actions)
            connection_vec.state = vector_state
            self.IDSet(connection_vec)

    def define_camera_controls(self):

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
                self.IUUpdate(self.camera_device, "CCD_TEMPERATURE", [current_value],
                              ["CCD_TEMPERATURE_VALUE"], Set=True)
                continue
            elif control["read_only"]:
                logger.warning(f"Read-only camera property: {control['name']}")
                continue
            number = INumber("CCD_"+control["name"].upper(), "%f", control["min"], control["max"],
                             1, current_value, label=control["name"])
            cam_controls.append(number)

        self.IDDef(INumberVector(cam_controls, self.camera_device, "CCD_CONTROLS", IPState.OK,
                                 IPerm.RW, label="Camera Controls"),
                   None)

    def park_mount(self):
        self.connection.rpc_command("scope_park")
        self.IUUpdate(self.scope_device, "TELESCOPE_PARK", [ISState.ON, ISState.OFF], ["PARK", "UNPARK"], Set=True)

    def unpark_mount(self):
        if self["TELESCOPE_PARK"]["PARK"].value == ISState.ON: # if currently parked
            self.connection.rpc_command("scope_move_to_horizon")
        else:
            logger.info("Seestar is already unparked.")
        self.IUUpdate(self.scope_device, "TELESCOPE_PARK", [ISState.OFF, ISState.ON], ["PARK", "UNPARK"], Set=True)

    def move_cw_by_angle(self, angle: int):
        """Rotate the telescope by `angle` degrees clockwise around the azimuth axis.
        Use negative values to turn counter-clockwise."""
        self.connection.rpc_command("scope_move_left_by_angle", params=[angle]) # even though it says left, positive numbers move it right (clockwise)

    def mount_is_moving(self) -> bool:
        """Checks if the mount is currently moving and returns True if it is."""

        result = self.connection.send_cmd_and_await_response("get_device_state",
                                                             params={"keys": ["mount"]})["result"]["mount"]
        return result["move_type"] != "none"

    def get_filter_position(self) -> int:
        if "WheelMove" in self.connection.event_states:
            if self.connection.event_states["WheelMove"]["state"] == "complete":
                return self.connection.event_states["WheelMove"]["position"]
            else:
                logger.warning("Filter wheel move not complete: latest status is '%s'",
                               self.connection.event_states["WheelMove"]["state"])
        response = self.connection.send_cmd_and_await_response("get_wheel_position")
        if response["code"]:
            logger.warning(f"Attempt to get filter wheel position returned code {response['code']}: {response}")
            return 3
        else:
            return response["result"]
    
    def set_filter_position(self, pos: int):
        if pos in range(3):
            response = self.connection.send_cmd_and_await_response("set_wheel_position", params=[pos])
        else:
            raise ValueError(f"Requested position {pos} does not exist")
        return response["code"]

    def get_focuser_position(self) -> int:
        if "FocuserMove" in self.connection.event_states:
            if self.connection.event_states["FocuserMove"]["state"] == "complete":
                return self.connection.event_states["FocuserMove"]["position"]
            else:
                logger.warning("Focuser move in progress: latest status is '%s'",
                               self.connection.event_states["FocuserMove"]["state"])
        response = self.connection.send_cmd_and_await_response("get_focuser_position",
                                                               params={"ret_obj": True})
        return response["result"]["step"]

    def move_focuser_absolute(self, position: int):
        status = self.connection.send_cmd_and_await_response("move_focuser",
                                                             params={"step": position,
                                                                     "ret_step": True})
        return status["code"]

    def move_focuser_relative(self, steps: int):
        current = self.get_focuser_position()
        code = self.move_focuser_absolute(current+steps)
        return code

    def start_auto_focus(self) -> bool:
        result = self.connection.send_cmd_and_await_response("start_auto_focuse")
        return result.get("code", 1) == 0
    
    def stop_auto_focus(self) -> bool:
        result = self.connection.send_cmd_and_await_response("stop_auto_focuse")
        return result.get("code", 1) == 0
    
    def focuser_loop_fn(self):
        self.IDMessage("Running focuser loop", msgtype="DEBUG", dev=self.focuser_device)

        last_focus_state = self.connection.event_states["FocuserMove"]
        if last_focus_state is None:
            # self.IDMessage("No focuser status has been received yet.", msgtype="WARN", dev=self.focuser_device)
            return

        position = last_focus_state["position"]

        pos_vec = self.IUUpdate(self.focuser_device, "ABS_FOCUS_POSITION", [position], ["FOCUS_ABSOLUTE_POSITION"])

        if last_focus_state["state"] == "complete":
            pos_vec.state = IPState.IDLE
        elif last_focus_state["state"] == "working":
            pos_vec.state = IPState.BUSY
        elif last_focus_state["state"] == "cancel":
            pos_vec.state = IPState.ALERT
        else:
            ...

        self.IDSet(pos_vec, msg=f"Focuser position is {position}")

    def filter_loop_fn(self):
        self.IDMessage("Running filter wheel loop", msgtype="DEBUG", dev=self.filterwheel_device)

        last_wheel_state = self.connection.event_states["WheelMove"]
        if last_wheel_state is None:
            # self.IDMessage("No filter wheel status has been received yet.", msgtype="WARN", dev=self.filterwheel_device)
            return

        position = last_wheel_state["position"]
        last_position = self["FILTER_SLOT"]["FILTER_SLOT_VALUE"].value

        if position == last_position:
            return

        pos_vec = self.IUUpdate(self.filterwheel_device, "FILTER_SLOT", [position], ["FILTER_SLOT_VALUE"])
        name_vec = self.IUUpdate(self.filterwheel_device, "FILTER_NAME", [self.filter_names[position]], ["FILTER_NAME_VALUE"])

        if last_wheel_state["state"] == "complete":
            pos_vec.state = IPState.IDLE
        elif last_wheel_state["state"] == "start":
            pos_vec.state = IPState.BUSY
        else:
            pos_vec.state = IPState.ALERT

        self.IDSet(pos_vec)
        self.IDSet(name_vec)

    def camera_loop_fn(self):
        self.IDMessage("Running camera loop", msgtype="DEBUG", dev=self.camera_device)
        result = self.connection.send_cmd_and_await_response("get_control_value", params=["Temperature"])["result"]
        self.IUUpdate(self.camera_device, 'CCD_TEMPERATURE', [result["value"]], ["CCD_TEMPERATURE_VALUE"], Set=True)

    def scope_loop_fn(self):
        self.IDMessage("Running telescope loop", msgtype="DEBUG", dev=self.scope_device)
        eq_coord = self.connection.send_cmd_and_await_response("scope_get_equ_coord")["result"]
        ra = eq_coord['ra']
        dec = eq_coord['dec']
        self.IUUpdate(self.scope_device, "EQUATORIAL_EOD_COORD", [ra, dec], ["RA", "DEC"], Set=True)
        horiz_coord = self.connection.send_cmd_and_await_response("scope_get_horiz_coord")
        alt, az = horiz_coord["result"]
        self.IUUpdate(self.scope_device, "HORIZONTAL_COORD", [alt, az], ["ALT", "AZ"], Set=True)

    @MultiDevice.repeat(2500)
    def do_loop(self):
        if not self.connected:
            return

        try:
            self.scope_loop_fn()
        except Exception as exc:
            self.IDMessage(f"Error in telescope loop: {exc}", msgtype="ERROR", dev=self.scope_device)
        try:
            self.camera_loop_fn()
        except Exception as exc:
            self.IDMessage(f"Error in camera loop: {exc}", msgtype="ERROR", dev=self.camera_device)
        try:
            self.focuser_loop_fn()
        except Exception as exc:
            self.IDMessage(f"Error in focuser loop: {exc}", msgtype="ERROR", dev=self.focuser_device)
        try:
            self.filter_loop_fn()
        except Exception as exc:
            self.IDMessage(f"Error in filter wheel loop: {exc}", msgtype="ERROR", dev=self.filterwheel_device)


if __name__ == "__main__":

    if THIS_FILE_PATH.name == "indi_seestar":
        seestar = SeestarDevice(DEFAULT_ADDR)
        seestar.start()
    else:
        scope_connection: RPCConnectionManager = get_connection_manager(DEFAULT_ADDR, CONTROL_PORT, "rpc")
        scope_connection.start_listening()
        camera_connection: ImageConnectionManager = get_connection_manager(DEFAULT_ADDR, IMAGING_PORT, "img")
        camera_connection.start_listening()

        time.sleep(1.0)

        if "--set-time" in sys.argv:
            now = time.localtime()
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
        scope_connection.rpc_command("get_device_state",
                                     params={"keys": ["device", "camera", "pi_status", "focuser"]})
        time.sleep(0.5)
        scope_connection.rpc_command("get_camera_info")
        time.sleep(0.5)
        scope_connection.rpc_command("pi_get_info")
        time.sleep(0.5)
        # scope_connection.rpc_command("scope_is_moving")
        # time.sleep(0.5)
        scope_connection.rpc_command("get_setting")
        time.sleep(0.5)
        # scope_connection.rpc_command("get_camera_exp_and_bin")
        # time.sleep(0.5)
        scope_connection.rpc_command("get_img_info")
