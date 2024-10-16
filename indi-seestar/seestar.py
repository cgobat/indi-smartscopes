#!/usr/bin/env python3

import os
import sys
import json
import time
import toml
import struct
import logging
import threading
from pathlib import Path
from collections import defaultdict
from astropy import units
from astropy.coordinates import SkyCoord
from pyindi.device import (device as Device, INumberVector, ISwitchVector, ITextVector,
                           INumber, ISwitch, IText, IPerm, IPState, ISState, ISRule)
sys.path.append(Path(__file__).parent.as_posix())
from socket_connections import connections_by_port, RPCConnectionManager, ImageConnectionManager, LogConnectionManager


CONTROL_PORT = 4700
IMAGING_PORT = 4800
LOGGING_PORT = 4801
DEFAULT_ADDR = "seestar.local"

logger = logging.getLogger(Path(__file__).stem)
logging.basicConfig(force=True, level=logging.DEBUG,
                    format="[%(levelname)s] %(message)s")


class SeestarScope(Device):

    def __init__(self, name=None, host=DEFAULT_ADDR):
        """
        Construct device with name and number
        """
        super().__init__(name=name)
        global connections_by_port
        self.connection = connections_by_port[host].get(CONTROL_PORT)
        if self.connection is None:
            self.connection = RPCConnectionManager(host, CONTROL_PORT)

    def ISGetProperties(self, device=None):
        """
        Property definitions are generated
        by initProperties and buildSkeleton. No
        need to do it here. """

        self.IDDef(INumberVector([INumber("RA", "%2.8f", 0, 24, 1, 0, label="RA"),
                                  INumber("DEC", "%2.8f", -90, 90, 1, -90, label="DEC")],
                                 self._devname, "EQUATORIAL_EOD_COORD", IPState.OK, IPerm.RW,
                                 label="Pointing Coordinates"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("CONNECT", ISState.OFF, "Connect"),
                                  ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                                 self._devname, "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="Connection"),
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
        
        if name == "EQUATORIAL_EOD_COORD":
            current = self["EQUATORIAL_EOD_COORD"]
            ra, dec = float(current['RA'].value), float(current['DEC'].value)
            
            self.IDMessage(f"Current pointing: RA={ra}, Dec={dec}")
            
            for index, value in enumerate(values):
                if value == 'RA':
                    ra = names[index]
                elif value == 'DEC':
                    dec = names[index]
                    
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
        """
        A switch has been updated from the client.
        """

        try:
            self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")

            if name == "CONNECTION":
                conn = self.IUUpdate(device, name, values, names)
                if conn["DISCONNECT"].value == ISState.ON:
                    self.connection.disconnect()
                    conn.state = IPState.IDLE
                elif conn["CONNECT"].value == ISState.ON:
                    self.connection.connect()
                    conn.state = IPState.OK

                self.IDSet(conn)

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
            
    @Device.repeat(2000)
    def do_repeat(self):
        """
        This function is called every 2000.
        """

        self.IDMessage("Running repeat function")
        
        try:
            cmd_id = self.connection.rpc_command("scope_get_equ_coord")
            result = self.connection.await_response(cmd_id)["result"]
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


class SeestarCamera(Device):
    
    def __init__(self, name=None, host="seestar.local"):
        super().__init__(name=name)
        global connections_by_port
        self.ctl_connection = connections_by_port[host].get(CONTROL_PORT)
        if self.ctl_connection is None:
            self.ctl_connection = RPCConnectionManager(host, CONTROL_PORT)
        self.img_connection = connections_by_port[host].get(IMAGING_PORT)
        if self.img_connection is None:
            self.img_connection = ImageConnectionManager(host, IMAGING_PORT)

    def ISGetProperties(self, device=None):

        cmd_id = self.ctl_connection.rpc_command("get_controls")
        control_defs = self.ctl_connection.await_response(cmd_id)["result"]
        cam_controls = []
        for control in control_defs:
            if control["name"].startswith("ISP_"):
                continue # skip ISP controls
            cmd_id = self.ctl_connection.rpc_command("get_control_value", params=[control["name"]])
            response = self.ctl_connection.await_response(cmd_id)
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


        self.IDDef(INumberVector([temperature], self._devname, "CCD_TEMPERATURE", IPState.OK, IPerm.RO, label="Camera Temperature"),
                   None)
        
        self.IDDef(INumberVector(cam_controls, self._devname, "CCD_CONTROLS", IPState.OK, IPerm.RW, label="Camera Controls"),
                   None)
        
        cmd_id = self.ctl_connection.rpc_command("get_camera_info")
        cam_info = self.ctl_connection.await_response(cmd_id)["result"]
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
                self.ctl_connection.rpc_command("start_exposure", params={})
                time.sleep(values[0])
                self.ctl_connection.rpc_command("stop_exposure")
                
            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}")
                

    def ISNewSwitch(self, device, name, values, names):
        """
        A switch has been updated from the client.
        """

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")

        try:
            if name == "CONNECTION":
                try:
                    conn = self.IUUpdate(device, name, values, names)
                    if conn["DISCONNECT"].value == ISState.ON:
                        # self.ctl_connection.rpc_command("close_camera")
                        self.ctl_connection.disconnect()
                        self.img_connection.disconnect()
                        conn.state = IPState.IDLE
                    elif conn["CONNECT"].value == ISState.ON:
                        self.ctl_connection.connect()
                        # self.ctl_connection.rpc_command("open_camera")
                        conn.state = IPState.OK

                    self.IDSet(conn)

                except Exception as error:
                    self.IDMessage(f"Error updating CONNECTION property: {error}")
                    raise
            else:
                prop = self.IUUpdate(device, name, values, names, Set=True)

        except Exception as error:
            self.IDMessage(f"Error updating {name} property: {error}")
    
    @Device.repeat(2000)
    def do_repeat(self):
        
        self.IDMessage("Running camera loop")

        try:
            result = self.ctl_connection.send_cmd_and_await_response("get_control_value", params=["Temperature"])["result"]
            self.IUUpdate(self._devname, 'CCD_TEMPERATURE', [result["value"]], ["CCD_TEMPERATURE_VALUE"], Set=True)
            
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")


class SeestarFocuser(Device):
    ...


class SeestarFilter(Device):
    ...


if __name__ == "__main__":

    scope_connection = RPCConnectionManager(DEFAULT_ADDR, CONTROL_PORT)
    scope_connection.start_listening()
    camera_connection = ImageConnectionManager(DEFAULT_ADDR, IMAGING_PORT)
    camera_connection.start_listening()
    log_connection = LogConnectionManager(DEFAULT_ADDR, LOGGING_PORT)
    log_connection.start_listening()

    # while not scope_connection.event_list:
    #     time.sleep(0.01)
    # initial_event = scope_connection.event_list[0]
    # t0 = float(initial_event["Timestamp"])
    # logger.debug(f"Initial event recorded at t={t0}")
    # now = time.gmtime()

    # scope_connection.rpc_command("pi_set_time",
    #                              params={"year": now.tm_year, "mon": now.tm_mon, "day": now.tm_mday,
    #                                      "hour": now.tm_hour, "min": now.tm_min, "sec": now.tm_sec,
    #                                      "time_zone": "Etc/UTC"})
    # scope_connection.rpc_command("get_view_state")
    # # time.sleep(0.1)
    # scope_connection.rpc_command("get_device_state", params={"keys": ["device", "camera", "pi_status"]})
    # time.sleep(0.1)
    # scope_connection.rpc_command("scope_get_ra_dec")
    # time.sleep(0.1)
    # scope_connection.rpc_command("get_camera_state")
    # time.sleep(0.1)
    # scope_connection.rpc_command("get_wheel_state")
    # time.sleep(0.1)
    # scope_connection.rpc_command("get_camera_info")
    # time.sleep(0.1)
    # scope_connection.rpc_command("pi_get_info")
    # time.sleep(0.1)
    # camera_connection.rpc_command("get_rtmp_config")
    # time.sleep(0.1)
    # scope_connection.rpc_command("scope_is_moving")
    # time.sleep(0.1)
    # scope_connection.rpc_command("get_setting")
    # time.sleep(0.1)
    # scope_connection.rpc_command("get_camera_exp_and_bin")
    # time.sleep(0.1)
    # scope_connection.rpc_command("get_control_value", params=["Exposure"])

    # scope = SeestarScope("MySeestar")
    # scope.start()
    # print(cam)
    # time.sleep(0.2)
    # print(cam["CCD_TEMPERATURE"].elements)
    # time.sleep(0.2)
    # print(cam["CCD_TEMPERATURE"].elements)
    # print(cam["CCD_CONTROLS"].elements)
