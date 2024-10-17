
import os
import abc
import json
import time
import atexit
import socket
import struct
import logging
import threading
from pathlib import Path
from collections import defaultdict

CONTROL_PORT = 4700
IMAGING_PORT = 4800
LOGGING_PORT = 4801
DEFAULT_ADDR = "seestar.local"

CONFIG_DIR = Path.home()/".indi_seestar"
CONFIG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger()
connections_by_port = defaultdict(dict)


def listen_send(address: str, port: int):
    global connections_by_port
    input_fifo = CONFIG_DIR/f"fifo_{address}_{port}_input.json"
    os.mkfifo(input_fifo.as_posix())
    sock = connections_by_port[address].get(port)
    if sock is None:
        sock = connections_by_port[address][port] = socket.create_connection((address, port))
    while True:
        try:
            with input_fifo.open("rb") as fifo:
                for line in fifo:
                    sock.sendall(line)
        except (socket.timeout, socket.error):
            logger.error(f"Socket connection to {address}:{port} broken. Reconnecting.")
            sock.close()
            sock = connections_by_port[address][port] = socket.create_connection((address, port))

def listen_recv(address: str, port: int):
    global connections_by_port
    output_fifo = CONFIG_DIR/f"fifo_{address}_{port}_output.pipe"
    os.mkfifo(output_fifo.as_posix())
    sock: socket.socket = connections_by_port[address].get(port)
    if sock is None:
        sock = connections_by_port[address][port] = socket.create_connection((address, port))
    while True:
        try:
            data = sock.recv(1024*64)
            with output_fifo.open("wb") as fifo:
                fifo.write(data)
        except (socket.timeout, socket.error):
            logger.error(f"Socket connection to {address}:{port} broken. Reconnecting.")
            sock.close()
            sock = connections_by_port[address][port] = socket.create_connection((address, port))

def cleanup():
    global connections_by_port
    for addr, socket_dict in connections_by_port.items():
        for port, sock in socket_dict:
            try:
                sock.close()
            except:
                pass
            finally:
                for fifo in CONFIG_DIR.glob(f"fifo_{addr}_{port}*"):
                    fifo.unlink()
                logger.debug(f"Closed socket and FIFOs for {addr}:{port}")

atexit.register(cleanup)


class BaseConnectionManager(abc.ABC):

    def __init__(self, address: str, port: int):
        self.address = str(address).strip()
        self.port = int(port)
        self.request_fifo = CONFIG_DIR/f"fifo_{self.address}_{self.port}_input.json"
        self.response_fifo = CONFIG_DIR/f"fifo_{self.address}_{self.port}_output.bin"
    
    @property
    def destination(self) -> str:
        return f"{self.address}:{self.port}"
    
    @property
    def socket(self) -> socket.socket:
        global connections_by_port
        return connections_by_port[self.address].get(self.port)
    
    @property
    def connected(self) -> bool:
        return self.request_fifo.exists() or self.response_fifo.exists()

    def connect(self):
        if self.request_fifo.exists() or self.response_fifo.exists():
            logger.info(f"Socket connection to {self.address}:{self.port} already established")
            self.connected = True
        else:
            self.connected = False
        # try:
        #     self.socket = socket.create_connection((self.address, self.port))
        #     logger.debug(f"Established socket connection with {self.destination}")
        #     self.connected = True
        # except:
        #     logger.exception(f"Error connecting to socket")
        #     self.connected = False
        return self.connected

    def disconnect(self):
        try:
            self.socket.close()
        except:
            pass
        self.request_fifo.unlink()
        self.response_fifo.unlink()
        self.connected = False
    
    def send_json(self, data: dict):
        try:
            json_str = json.dumps(data).encode()
            with self.request_fifo.open("wb") as fifo:
                fifo.write(json_str)
        except:
            logger.exception(f"Failed to write JSON to FIFO {self.request_fifo}")

    def receive_bytes(self):
        with self.response_fifo.open("rb") as fifo:
            for line in fifo:
                return line

    def start_listening(self) -> threading.Thread:
        if not self.connected:
            logger.error("Socket not connected. Can't listen for messages.")
            return None
        thread = threading.Thread(target=self.receive_loop)
        thread.start()
        logger.debug(f"Started listening for messages from {self.destination}")
        return thread
    
    @abc.abstractmethod
    def receive_loop():
        ...


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
            with self.response_fifo.open("rb") as fifo:
                for data in fifo:
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

# this block will be executed whenever this file is run or anything here is imported
for port in (CONTROL_PORT, IMAGING_PORT, LOGGING_PORT):
    input_fifo = CONFIG_DIR/f"fifo_{DEFAULT_ADDR}_{port}_input.json"
    output_fifo = CONFIG_DIR/f"fifo_{DEFAULT_ADDR}_{port}_output.pipe"
    if not input_fifo.exists():
        in_thread = threading.Thread(target=listen_send, args=(DEFAULT_ADDR, port))
        in_thread.start()
        logger.info(f"{__file__} started listening for outgoing data from {DEFAULT_ADDR}:{port}")
    if not output_fifo.exists():
        out_thread = threading.Thread(target=listen_recv, args=(DEFAULT_ADDR, port))
        out_thread.start()
        logger.info(f"{__file__} started listening for incoming data from {DEFAULT_ADDR}:{port}")
