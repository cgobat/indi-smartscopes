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
from pyindi.device import (device as Device, INumberVector, ISwitchVector,
                           INumber, ISwitch, IPerm, IPState, ISState, ISRule)


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
                    elif "Event" in parsed:
                        self.event_list.append(parsed)
                    else:
                        logger.warning("Got non-RPC and non-Event message!")
                    logger.debug(f"Received from {self.destination}:\n{json.dumps(parsed, indent=2, sort_keys=False)}")
            
            time.sleep(1)

    def start_listening(self) -> threading.Thread:
        if not self.connected:
            self.connect()
        thread = threading.Thread(target=self.receive_loop)
        thread.start()
        logger.debug(f"Started listening for messages from {self.destination}")
        return thread


class SeestarDevice(Device):

    def __init__(self, name=None, number=1):
        """
        Construct device with name and number
        """
        super().__init__(name=name)
        self.number = number
        self.url = f'http://localhost:5555/api/v1/telescope/{number}/action'
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }

    def ISGetProperties(self, device=None):
        """
        Property definitions are generated
        by initProperties and buildSkeleton. No
        need to do it here. """
        ra =  INumber( "RA", "%2.8f", 0, 24, 1, 0, label="RA" )
        dec = INumber( "DEC", "%2.8f", -90, 90, 1, -90, label="DEC" )
        coord = INumberVector([ra, dec], self._devname, "EQUATORIAL_EOD_COORD",
                              IPState.OK, IPerm.RW, label="EQUATORIAL_EOD_COORD")
 
        connect = ISwitch("CONNECT", ISState.OFF, "Connect", )
        disconnect = ISwitch("DISCONNECT", ISState.ON, "Disconnect")
        conn = ISwitchVector([connect, disconnect], self._devname, "CONNECTION",
                IPState.IDLE, ISRule.ONEOFMANY, 
                IPerm.RW, label="Connection")
                
        slew = ISwitch("SLEW", ISState.ON, "Slew", )
        track = ISwitch("TRACK", ISState.OFF, "Track")
        sync = ISwitch("SYNC", ISState.OFF, "Sync")
        oncoordset = ISwitchVector([slew, track, sync], self._devname, "ON_COORD_SET",
                IPState.IDLE, ISRule.ONEOFMANY, 
                IPerm.RW, label="On coord set")
                
        self.IDDef(coord, None)
        self.IDDef(conn, None)
        self.IDDef(oncoordset, None)
        

    def ISNewText(self, device, name, names, values):
        """
        A text vector has been updated from 
        the client. 
        """
        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        self.IUUpdate(device, name, names, values, Set=True)

    def ISNewNumber(self, device, name, names, values):
        """
        A number vector has been updated from the client.
        """
        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        
        if name == "EQUATORIAL_EOD_COORD":
            current = self.__getitem__("EQUATORIAL_EOD_COORD")
            ra, dec = float(current['RA'].value), float(current['DEC'].value)
            
            self.IDMessage(f"Current pointing: RA={ra}, Dec={dec}")
            
            for index, value in enumerate(values):
                if value == 'RA':
                    ra = names[index]
                elif value == 'DEC':
                    dec = names[index]
                    
            self.IDMessage(f"Requested RA/Dec: ({ra}, {dec})")

            switch = self.__getitem__('ON_COORD_SET')
            if switch['SLEW'].value == 'On' or switch['TRACK'].value == 'On':
                # Slew/GoTo requested
                if self.goToInProgress():
                    self.terminateGoTo()
                target = SkyCoord(ra * units.hourangle, dec * units.deg)
                ra_hms = target.ra.to_string(unit=units.hourangle, sep=('h', 'm', 's'))
                dec_dms = target.dec.to_string(unit=units.deg, sep=('d', 'm', 's'))
                
                self.IDMessage(f"Requested RA/Dec (str): ({ra_hms}, {dec_dms})")
                
                payload = {
                    "Action": "goto_target",
                    "Parameters": f'{{"target_name":"Stellarium Target", "ra":"{ra_hms}", "dec":"{dec_dms}", "is_j2000":false}}',
                    "ClientID": "1",
                    "ClientTransactionID": "999"
                }
            else:
                # Sync requested
                payload = {
                    "Action": "method_sync",
                    "Parameters": f'{{"method":"scope_sync","params":[{ra}, {dec}]}}',
                    "ClientID": "1",
                    "ClientTransactionID": "999"
                }
            
            try:
                response = requests.put(self.url, data=payload, headers=self.headers)
                
                print(response.json())
                
            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}")
                

    def ISNewSwitch(self, device, name, names, values):
        """
        A switch has been updated from the client.
        """

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")

        if name == "CONNECTION":
            try:
                conn = self.IUUpdate(device, name, names, values)
                if conn["CONNECT"].value == 'Off':
                    conn.state = "Idle"
                else:
                    conn.state = "Ok"

                self.IDSet(conn)

            except Exception as error:
                self.IDMessage(f"Error updating CONNECTION property: {error}")
                raise
        else:
            try:
                prop = self.IUUpdate(device, name, names, values)
                self.IDSet(prop)
            except Exception as error:
                self.IDMessage(f"Error updating {name} property: {error}")
                raise
            
    @Device.repeat(2000)
    def do_repeat(self):
        """
        This function is called every 2000.
        """

        conn = self.__getitem__("CONNECTION")
        if conn["CONNECT"].value == 'Off':
            # return
            pass
            
        self.IDMessage("Running repeat function")
        
        payload = {
            "Action": "method_sync",
            "Parameters": "{\"method\":\"scope_get_equ_coord\"}",
            "ClientID": "1",
            "ClientTransactionID": "999"
        }
        
        try:
            response = requests.put(self.url, data=payload, headers=self.headers)

            # parse response and update number vector
            json = response.json()
            result = json['Value']['result']
            ra = result['ra']
            dec = result['dec']
            self.IUUpdate(self._devname, 'EQUATORIAL_EOD_COORD', [ra, dec], ['RA', 'DEC'], Set=True)
            
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")

    def goToInProgress(self):
        """
        Return true if a GoTo is in progress, false otherwise
        """
        payload = {
            "Action": "method_sync",
            "Parameters": "{\"method\":\"get_view_state\"}",
            "ClientID": "1",
            "ClientTransactionID": "999"
        }
        
        try:
            response = requests.put(self.url, data=payload, headers=self.headers)
            json = response.json()
            result = json['Value']['result']
            return result['View']['stage'] == 'AutoGoto'
        
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")
        
    def terminateGoTo(self):
        """
        Terminates current GoTo operation
        """
        payload = {
            "Action": "method_sync",
            "Parameters": "{\"method\":\"iscope_stop_view\",\"params\":{\"stage\":\"AutoGoto\"}}",
            "ClientID": "1",
            "ClientTransactionID": "999"
        }
        
        try:
            response = requests.put(self.url, data=payload, headers=self.headers)
        
        except Exception as error:
            self.IDMessage(f"Error terminating GoTo: {error}")

name = os.environ['INDIDEV']
number = int(os.environ['INDICONFIG'])  #hijack to obtain device number
ss = SeestarDevice(name, number)
ss.start() 
 