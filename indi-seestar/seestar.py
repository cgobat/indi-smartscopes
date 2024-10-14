#!/usr/bin/env python3

import os
import sys
import json
import time
import toml
import socket
import logging
import threading
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path.cwd().parent))
from astropy import units
from astropy.coordinates import SkyCoord
from pyindi.device import (device as Device, INumberVector, ISwitchVector, ITextVector,
                           INumber, ISwitch, IText, IPerm, IPState, ISState, ISRule)


CONTROL_PORT = 4700
IMAGING_PORT = 4800
LOGGING_PORT = 4801
DEFAULT_ADDR = "seestar.local"
connections_by_port = defaultdict(dict)

logger = logging.getLogger(Path(__file__).stem)
logging.basicConfig(force=True, level=logging.DEBUG,
                    format="[%(levelname)s] %(message)s")


class ConnectionManager:

    def __init__(self, address: str, port: int):
        global connections_by_port
        self.address = str(address).strip()
        self.port = int(port)
        if self.port in connections_by_port[self.address]:
            raise ConnectionError(f"Connection to {self.destination} already exists.")
        self.socket = None
        self.connected = False
        self.cmd_id = 100
        self.rpc_responses = {}
        self.event_list = []
        connections_by_port[self.address][self.port] = self
    
    @property
    def destination(self) -> str:
        return f"{self.address}:{self.port}"

    def connect(self):
        try:
            self.socket = socket.create_connection((self.address, self.port))
            logger.debug(f"Established socket connection with {self.destination}")
            self.connected = True
        except:
            logger.exception(f"Error connecting to socket")
            self.connected = False
        return self.connected
    
    def disconnect(self):
        self.socket.close()
        self.connected = False

    def send_rpc(self, data: dict):
        try:
            json_str = json.dumps(data).encode()
            self.socket.sendall(json_str + b'\r\n')
        except:
            logger.exception("RPC send failed due to exception") 
    
    def rpc_command(self, command: str, **kwargs):
        payload = {"id": self.cmd_id, "method": command}
        payload.update(kwargs)
        self.send_rpc(payload)
        self.cmd_id += 1
        return payload["id"]

    def receive_msg_str(self):
        try:
            if self.socket is None or not self.connected:
                raise socket.error("Socket not initialized")
            data = self.socket.recv(1024 * 8)
        except socket.timeout:
            logger.warning("Socket timeout")
            return None
        except socket.error as e:
            logger.exception("Error reading socket")
            if self.socket is not None:
                self.disconnect()
            if self.connect():
                return self.receive_msg()
            return None
        
        try:
            return data.decode()
        except UnicodeDecodeError:
            logger.warning(f"Failed to decode data: {data}")
            return None
    
    @staticmethod
    def parse_json(data: str) -> dict:
        return json.loads(data.strip())
    
    def receive_loop(self):
        remaining = ""
        while True:
            data = self.receive_msg_str()
            if data:
                remaining += data
                first_idx = remaining.find("\r\n")

                while first_idx >= 0:
                    message = remaining[:first_idx]
                    remaining = remaining[first_idx+2:]
                    parsed = self.parse_json(message)
                    first_idx = remaining.find("\r\n")
                    if "jsonrpc" in parsed:
                        self.rpc_responses[parsed["id"]] = parsed
                        if parsed.get("code", 0):
                            logger.warning(f"Got non-zero return code in response to RPC command '{parsed['method']}' (ID: {parsed['id']})")
                    elif "Event" in parsed:
                        self.event_list.append(parsed)
                    else:
                        logger.warning("Got non-RPC and non-Event message!")
                    logger.debug(f"Received from {self.destination}:\n{json.dumps(parsed, indent=2, sort_keys=False)}")
            
            time.sleep(1)
    
    def await_response(self, rpc_id: int):
        while rpc_id not in self.rpc_responses:
            time.sleep(0.01)
        return self.rpc_responses[rpc_id]
    
    def send_cmd_and_await_response(self, command, **kwargs) -> dict:
        cmd_id = self.rpc_command(command, **kwargs)
        return self.await_response(cmd_id)

    def start_listening(self) -> threading.Thread:
        if not self.connected:
            self.connect()
        if not self.connected:
            logger.error("Socket not connected. Can't listen for messages.")
            return None
        thread = threading.Thread(target=self.receive_loop)
        thread.start()
        logger.debug(f"Started listening for messages from {self.destination}")
        return thread


class SeestarScope(Device):

    def __init__(self, name=None, host=DEFAULT_ADDR):
        """
        Construct device with name and number
        """
        super().__init__(name=name)
        global connections_by_port
        self.connection = connections_by_port[host].get(CONTROL_PORT)
        if self.connection is None:
            self.connection = ConnectionManager(host, CONTROL_PORT)

    def ISGetProperties(self, device=None):
        """
        Property definitions are generated
        by initProperties and buildSkeleton. No
        need to do it here. """

        self.IDDef(INumberVector([INumber( "RA", "%2.8f", 0, 24, 1, 0, label="RA" ),
                                  INumber( "DEC", "%2.8f", -90, 90, 1, -90, label="DEC" )],
                                 self._devname, "EQUATORIAL_EOD_COORD", IPState.OK, IPerm.RW,
                                 label="EQUATORIAL_EOD_COORD"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("CONNECT", ISState.OFF, "Connect", ),
                                  ISwitch("DISCONNECT", ISState.ON, "Disconnect")],
                                 self._devname, "CONNECTION", IPState.IDLE, ISRule.ONEOFMANY,
                                 IPerm.RW, label="Connection"),
                   None)

        self.IDDef(ISwitchVector([ISwitch("SLEW", ISState.ON, "Slew", ),
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

        self.IDDef(ISwitchVector([ISwitch("DEW_HEATER_STATE", ISState.ON if status["result"]["setting"]["heater_enable"] else ISState.OFF)],
                                 self._devname, "DEW_HEATER", state=IPState.OK, rule=ISRule.ATMOST1, perm=IPerm.RW, label="Dew Heater Enable"),
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
                if heater["DEW_HEATER_STATE"] == ISState.OFF:
                    self.connection.rpc_command("set_setting", params=[{"heater_enable": False}])
                elif heater["DEW_HEATER_STATE"] == ISState.ON:
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
    ...


class SeestarFocuser(Device):
    ...


class SeestarFilter(Device):
    ...


name = os.environ['INDIDEV']
number = int(os.environ['INDICONFIG'])  #hijack to obtain device number
ss = SeestarScope(name, number)
ss.start() 
 