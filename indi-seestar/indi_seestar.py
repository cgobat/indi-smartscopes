#!/usr/bin/env python3

import sys
import time
import tzlocal
import logging
from pathlib import Path
from collections import defaultdict

from pyindi.device import (device as IDevice, INumberVector, ISwitchVector, ITextVector,
                           INumber, ISwitch, IText, IPerm, IPState, ISState, ISRule)

THIS_FILE_PATH = Path(__file__) # leave symlinks as-is/unresolved
sys.path.append(THIS_FILE_PATH.resolve().parent.as_posix()) # resolve source directory

from socket_connections import (DEFAULT_ADDR, CONTROL_PORT, IMAGING_PORT, LOGGING_PORT,
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

    def ISGetProperties(self, device=None):

        self.IDDef(ISwitchVector([ISwitch("CONNECT", ISState.OFF, "Connect"),
                                  ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                                 self._devname, "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="Connection"),
                   None)
    
    def handle_connection_update(self, devname, actions: "list[str]", states: "list[ISState]"):
        conn = self.IUUpdate(devname, "CONNECTION", states, actions)
        if conn["DISCONNECT"].value == ISState.ON:
            self.connection.disconnect() # also stops listening and heartbeat
            conn.state = IPState.IDLE
        elif conn["CONNECT"].value == ISState.ON:
            self.connection.connect() # automatically starts heartbeat
            self.connection.start_listening()
            conn.state = IPState.OK

        self.IDSet(conn)


class SeestarScope(SeestarCommon):

    def __init__(self, name=None, host=DEFAULT_ADDR):
        super().__init__(name, host)

    def ISGetProperties(self, device=None):
        """Called when client or indiserver sends `getProperties`."""

        get_time = self.connection.send_cmd_and_await_response("pi_get_time")
        utc_time = self.connection.convert_timestamp(get_time["Timestamp"])

        self.IDDef(ITextVector([IText("UTC", utc_time.replace(tzinfo=None).isoformat()),
                                IText("OFFSET", "+0000")],
                               self._devname, "TIME_UTC", IPState.IDLE, IPerm.RW, timeout=1),
                   None)

        pointing = self.connection.send_cmd_and_await_response("scope_get_equ_coord")

        self.IDDef(INumberVector([INumber("RA", "%2.8f", 0, 24, 1, pointing["result"]["ra"], label="RA"),
                                  INumber("DEC", "%2.8f", -90, 90, 1, pointing["result"]["dec"], label="Dec")],
                                 self._devname, "EQUATORIAL_EOD_COORD", IPState.OK, IPerm.RW,
                                 label="Pointing Coordinates"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("SLEW", ISState.ON, "Slew"),
                                  ISwitch("TRACK", ISState.OFF, "Track"),
                                  ISwitch("SYNC", ISState.OFF, "Sync")],
                                 self._devname, "ON_COORD_SET", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="On coord set"),
                   None)
        
        status = self.connection.send_cmd_and_await_response("get_device_state",
                                                             params={"keys": ["device", "setting", "pi_status"]})
        device = status["result"]["device"]
        self.IDDef(INumberVector([INumber("TELESCOPE_APERTURE", format="%f", min=0, max=10000, step=1,
                                          value=device["focal_len"]/device["fnumber"], label="Aperture (mm)"),
                                  INumber("TELESCOPE_FOCAL_LENGTH", format="%f", min=0, max=100000, step=1,
                                          value=device["focal_len"], label="Focal Length (mm)")],
                                 self._devname, "TELESCOPE_INFO", IPState.IDLE, IPerm.RO, label="Optical Properties"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("DEW_HEATER_ENABLED", ISState.ON if status["result"]["setting"]["heater_enable"] else ISState.OFF),
                                  ISwitch("DEW_HEATER_DISABLED", ISState.OFF if status["result"]["setting"]["heater_enable"] else ISState.ON)],
                                 self._devname, "DEW_HEATER", state=IPState.OK, rule=ISRule.ONEOFMANY, perm=IPerm.RW, label="Dew Heater Enable"),
                   None)

    def ISNewText(self, device, name, values, names):
        """A text vector has been updated from the client."""

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        self.IUUpdate(device, name, values, names, Set=True)

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
                if self.goToInProgress():
                    self.terminateGoTo()
                cmd = "iscope_start_view"
                params = {"mode": "star", "target_ra_dec": [ra, dec], "target_name": "Stellarium Target", "lp_filter": False}
            elif switch["SYNC"].value == ISState.ON:
                # Sync requested
                cmd = "scope_sync"
                params = [ra, dec]

            try:
                response = self.connection.send_cmd_and_await_response(cmd, params=params)
                self.IDMessage(f"Set RA/Dec to {(ra, dec)}")

            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}")

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

            elif name == "DEW_HEATER":
                heater = self.IUUpdate(device, name, values, names)
                if heater["DEW_HEATER_DISABLED"] == ISState.ON:
                    self.connection.rpc_command("set_setting", params=[{"heater_enable": False}])
                elif heater["DEW_HEATER_ENABLED"] == ISState.ON:
                    self.connection.rpc_command("set_setting", params=[{"heater_enable": True}])

            else:
                prop = self.IUUpdate(device, name, values, names)
                self.IDSet(prop)

        except Exception as error:
            self.IDMessage(f"Error updating {name} property: {error}")
            raise
            
    @IDevice.repeat(2000) # ms
    def do_repeat(self):
        """Tasks to repeat every other second."""

        self.IDMessage("Running repeat function")
        
        try:
            result = self.connection.send_cmd_and_await_response("scope_get_equ_coord")["result"]
            ra = result['ra']
            dec = result['dec']
            self.IUUpdate(self._devname, 'EQUATORIAL_EOD_COORD', [ra, dec], ['RA', 'DEC'], Set=True)
            
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")

    def goToInProgress(self):
        """
        Return true if a GoTo is in progress, false otherwise
        """
        
        try:
            result = self.connection.send_cmd_and_await_response("get_view_state")["result"]
            return result['View']['stage'] == 'AutoGoto'
        
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")
        
    def terminateGoTo(self):
        """
        Terminates current GoTo operation
        """
        
        try:
            self.connection.rpc_command("iscope_stop_view", params={"stage": "AutoGoto"})
        
        except Exception as error:
            self.IDMessage(f"Error terminating GoTo: {error}")


class SeestarCamera(SeestarCommon):
    
    def __init__(self, name=None, host=DEFAULT_ADDR):
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

    scope_connection = get_connection_manager(DEFAULT_ADDR, CONTROL_PORT, "rpc")
    scope_connection.start_listening()
    # camera_connection = get_connection_manager(DEFAULT_ADDR, IMAGING_PORT, "img")
    # camera_connection.start_listening()
    # log_connection = get_connection_manager(DEFAULT_ADDR, LOGGING_PORT, "log")
    # log_connection.start_listening()

    # while not scope_connection.event_list:
    #     time.sleep(0.01)
    # initial_event = scope_connection.event_list[0]
    # t0 = float(initial_event["Timestamp"])
    # logger.debug(f"Initial event recorded at t={t0}")
    now = time.localtime()

    if THIS_FILE_PATH.name == "indi_seestar_scope":
        scope = SeestarScope("MySeestar")
        scope.start()
    elif THIS_FILE_PATH.name == "indi_seestar_ccd":
        camera = SeestarCamera("MySeestar")
        camera.start()
    elif THIS_FILE_PATH.name == "indi_seestar_focuser":
        focuser = SeestarFocuser("MySeestar")
        focuser.start()
    elif THIS_FILE_PATH.name == "indi_seestar_filterwheel":
        filter_wheel = SeestarFilter("MySeestar")
        filter_wheel.start()
    else:
        if "--set-time" in sys.argv:
            logger.info(f"Setting Seestar time to {now}")
            scope_connection.rpc_command("pi_set_time",
                                         params={"year": now.tm_year, "mon": now.tm_mon, "day": now.tm_mday,
                                                 "hour": now.tm_hour, "min": now.tm_min, "sec": now.tm_sec,
                                                 "time_zone": tzlocal.get_localzone_name()})
            time.sleep(0.5)
        # scope_connection.rpc_command("get_view_state")
        time.sleep(1.0)
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
