
import abc
import json
import time
import socket
import struct
import logging
import threading
from pathlib import Path
from collections import defaultdict

logger = logging.getLogger()
connections_by_port = defaultdict(dict)

class BaseConnectionManager(abc.ABC):

    def __init__(self, address: str, port: int):
        self.address = str(address).strip()
        self.port = int(port)
        global connections_by_port
        if self.port in connections_by_port[self.address]:
            raise ConnectionError(f"Connection to {self.destination} already exists.")
        self.socket = None
        self.connected = False
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
    
    def send_json(self, data: dict):
        try:
            json_str = json.dumps(data).encode()
            self.socket.sendall(json_str + b'\r\n')
        except:
            logger.exception("Failed to send JSON due to exception") 

    def receive_bytes(self, size=1024*8):
        try:
            if self.socket is None or not self.connected:
                raise socket.error("Socket not initialized")
            data = self.socket.recv(size)
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
        return data

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


class RPCConnectionManager(BaseConnectionManager):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmd_id = 100
        self.rpc_responses = {}
        self.event_list = []

    def rpc_command(self, command: str, **kwargs):
        payload = {"id": self.cmd_id, "method": command}
        payload.update(kwargs)
        self.send_json(payload)
        self.cmd_id += 1
        return payload["id"]
    
    @staticmethod
    def parse_json(data: "str|bytes") -> dict:
        return json.loads(data.strip())
    
    def receive_loop(self):
        remaining = b''
        while True:
            data = self.receive_bytes()
            if data:
                remaining += data
                first_idx = remaining.find(b'\r\n')

                while first_idx >= 0:
                    message = remaining[:first_idx]
                    remaining = remaining[first_idx+2:]
                    parsed = self.parse_json(message)
                    first_idx = remaining.find(b'\r\n')
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


class RawConnectionManager(BaseConnectionManager):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmd_id = 1000
        self.raw_data = b''

    @staticmethod
    def parse_header(data: bytes):
        pack_fmt = ">HHHIHHBBHH"
        return dict(zip(["s1", "s2", "s3", "size", "s5", "s6", "code", "id", "width", "height"],
                        struct.unpack(pack_fmt, data[:struct.calcsize(pack_fmt)])))

    def receive_loop(self):
        while True:
            if not self.connected:
                time.sleep(1)
                continue
            header_bytes = self.receive_bytes(80)
            if len(header_bytes) >= 20:
                header = self.parse_header()
                data = self.read_bytes(header["size"])
                if header["id"] == self.cmd_id:
                    self.raw_data = data
                else:
                    logger.warning(f"Message ID {header['id']} is out of sync with current command ID {self.cmd_id}")
            elif header_bytes:
                logger.warning(f"Received less than 20B: {header_bytes}")


class ImageConnectionManager(RawConnectionManager, RPCConnectionManager):

    def request_image():
        ...


class LogConnectionManager(RawConnectionManager):

    def request_log(self):
        self.send_json({"id": self.cmd_id+1, "method": "get_server_log"})
        self.cmd_id += 1

    def get_log_single(self):
        self.start_listening()
        self.request_log()
        while not self.raw_data:
            logger.debug("Waiting for log message...")
            time.sleep(2)
        self.disconnect()
        return self.raw_data
