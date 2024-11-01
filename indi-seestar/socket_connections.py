
import os
import abc
import json
import pytz
import time
import fcntl
import atexit
import select
import socket
import struct
import logging
import requests
import threading
import datetime as dt
from pathlib import Path
from collections import defaultdict

logging.basicConfig(force=True, level=logging.DEBUG,
                    format="[%(levelname)s] %(message)s")

GUIDER_PORT  = 4400
CONTROL_PORT = 4700
IMAGING_PORT = 4800
LOGGING_PORT = 4801
DEFAULT_ADDR = "seestar.local"

MSG_END = b'\r\n'

CONFIG_DIR = Path.home()/".indi_seestar"
CONFIG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger()
sockets_by_port = defaultdict(dict)
lock_file_path = CONFIG_DIR/"seestar_socket_pid.lock"
lock_fd = None

def get_socket(address: str, port: int) -> socket.socket:
    global sockets_by_port
    sock = sockets_by_port[address].get(port)
    if sock is None or sock._closed or sock.fileno() == -1:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(None)
        sock.connect((address, port))
        sockets_by_port[address][port] = sock
        logger.debug(f"Established new socket connection to {address}:{port}")
    return sock

def cleanup():
    global sockets_by_port, lock_fd, lock_file_path
    if lock_fd is not None:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_file_path.unlink(missing_ok=True)
    for addr, socket_dict in sockets_by_port.items():
        for port, sock in socket_dict.items():
            try:
                sock.close()
            except:
                pass

atexit.register(cleanup)


class BaseConnectionManager(abc.ABC):

    def __init__(self, address: str, port: int):
        self.address = str(address).strip()
        self.port = int(port)
        self.socket = None
        self.connected = False
        self._do_listen = False

    @property
    def destination(self) -> str:
        return f"{self.address}:{self.port}"

    def connect(self):
        for i in range(3):
            try:
                self.socket = get_socket(self.address, self.port)
                self.connected = True
                break
            except ConnectionError:
                logger.warning(f"Error getting/connecting socket. {2-i} tries left.")
                time.sleep(0.1)
        else:
            self.connected = False
        if self.connected:
            self.start_heartbeat()

    def disconnect(self):
        try:
            self.socket.close()
        except:
            pass # hope for the best...
        self.connected = self._do_listen = self._do_heartbeat = False

    def send_json(self, data: dict):
        if not self.connected:
            logger.error("Socket not connected. Can't send JSON.")
            return
        try:
            json_str = json.dumps(data).encode()
            self.socket.sendall(json_str+b'\r\n')
        except:
            logger.exception(f"Failed to send JSON to socket {self.socket}")

    def start_listening(self) -> threading.Thread:
        if self._do_listen:
            logger.warning(f"Already listening on socket {self.socket}")
            return
        if not self.connected:
            self.connect()
        if not self.connected: # still not connected even after trying
            logger.error("Socket not connected. Can't listen for messages.")
            return None
        thread = threading.Thread(target=self.receive_loop)
        self._do_listen = True
        thread.start()
        logger.debug(f"Started listening on socket {self.socket}")
        return thread

    def stop_listening(self):
        self._do_listen = False

    def start_heartbeat(self) -> threading.Thread:
        thread = threading.Thread(target=self.heartbeat_loop)
        self._do_heartbeat = True
        thread.start()
        return thread

    def stop_heartbeat(self):
        self._do_heartbeat = False

    def http_get(self, endpoint) -> requests.Response:
        header = {"Connection": "Keep-Alive",
                  "Accept-Encoding": "gzip",
                  "User-Agent": "okhttp/4.2.2"}
        response = requests.get(f"http://{self.address}{endpoint}", headers=header)
        return response

    @abc.abstractmethod
    def receive_loop(self):
        ...

    @abc.abstractmethod
    def heartbeat_loop(self):
        ...


class RPCConnectionManager(BaseConnectionManager):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmd_id = 100
        self.rpc_responses = {}
        self.event_states = defaultdict(lambda : None)

    def rpc_command(self, command: str, **kwargs):
        """Send `command` as a JSON RPC message with additional arguments specified by `kwargs`."""

        payload = {"id": self.cmd_id, "method": command}
        payload.update(kwargs)
        self.send_json(payload)
        self.cmd_id += 1
        return payload["id"]

    def convert_timestamp(self, timestamp: "str|float"):
        time_responses = [response for _, response in self.rpc_responses.items() if response["method"]=="pi_get_time"]
        if not time_responses:
            time_responses = [self.send_cmd_and_await_response("pi_get_time")]
        if time_responses:
            most_recent = max(time_responses, key=lambda r: float(r["Timestamp"]))
            tdict = most_recent["result"]
            ref_time = dt.datetime(year=tdict["year"], month=tdict["mon"], day=tdict["day"], hour=tdict["hour"],
                                   minute=tdict["min"], second=tdict["sec"], tzinfo=pytz.timezone(tdict["time_zone"]))
            epoch = ref_time - dt.timedelta(seconds=float(most_recent["Timestamp"])) # time at Timestamp=0
            return (epoch + dt.timedelta(seconds=float(timestamp))).astimezone(dt.timezone.utc)
        return float(timestamp)

    @staticmethod
    def parse_json(data: "str|bytes") -> dict:
        return json.loads(data.strip())

    def receive_loop(self):
        remaining = b''
        poller = select.poll()
        poller.register(self.socket, select.POLLIN)
        while self._do_listen:
            events = dict(poller.poll())
            if events:
                data = self.socket.recv(1024*64)
                remaining += data

                while MSG_END in remaining:
                    message, *others = remaining.split(MSG_END)
                    remaining = MSG_END.join(others)
                    parsed = self.parse_json(message)
                    if "jsonrpc" in parsed:
                        self.rpc_responses[parsed["id"]] = parsed
                        if parsed.get("code", 0):
                            logger.warning(f"Got non-zero return code in response to RPC command '{parsed['method']}' (ID: {parsed['id']})")
                    elif "Event" in parsed:
                        event_name = parsed["Event"]
                        if event_name == "PiStatus":
                            if "temp" in parsed:
                                event_name += "_temperature"
                            elif "battery_capacity" in parsed:
                                event_name += "_battery"
                            else:
                                event_name += "_other"
                        self.event_states[event_name] = parsed
                    else:
                        logger.warning("Got non-RPC and non-Event message!")
                    logger.debug(f"Read message from {':'.join(map(str, self.socket.getpeername()))}:"
                                 f"\n{json.dumps(parsed, indent=2, sort_keys=False)}")
            else:
                time.sleep(0.2)

    def heartbeat_loop(self):
        while self._do_heartbeat:
            if not self.connected:
                try:
                    self.connect()
                except:
                    time.sleep(3)
                    continue
            self.rpc_command("test_connection", id="heartbeat")
            time.sleep(3)

    def await_response(self, rpc_id: int):
        """Wait until a response corresponding to `rpc_id` appears, then return it."""

        while rpc_id not in self.rpc_responses:
            time.sleep(0.01)
        return self.rpc_responses[rpc_id]

    def send_cmd_and_await_response(self, command, **kwargs) -> dict:
        """Convenience function that chains `rpc_command()` with `await_response()` and returns the result."""

        cmd_id = self.rpc_command(command, **kwargs)
        return self.await_response(cmd_id)


class RawConnectionManager(BaseConnectionManager):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmd_id = 1000
        self.raw_data = b''

    @staticmethod
    def parse_header(data: bytes):
        pack_fmt = ">HHHIHHBBHH" # H=u2, I=u4, B=u1
        return dict(zip(["s1", "s2", "s3", "size", "s5", "s6", "code", "id", "width", "height"],
                        struct.unpack(pack_fmt, data[:struct.calcsize(pack_fmt)])))

    def receive_loop(self):
        poller = select.poll()
        poller.register(self.socket, select.POLLIN)
        while self._do_listen:
            events = dict(poller.poll())
            if not events:
                time.sleep(0.2)
                continue
            header_bytes = self.socket.recv(80)
            if len(header_bytes) >= 20 and header_bytes.startswith(b'\x03\xc3\x00\x02'):
                header = self.parse_header(header_bytes)
                data = self.socket.recv(header["size"])
                logger.debug(f"Received {len(data)}B message ({header=})")
                if header["id"] == 2: # heartbeat/connection confirmation
                    continue
                while len(data) < header["size"]:
                    data += self.socket.recv(header["size"]-len(self.raw_data))
                    logger.debug(f"Raw data now has {len(data)} bytes")
                self.raw_data = data
            elif header_bytes:
                remaining = self.socket.recv(1024*64)
                data = header_bytes + remaining
                logger.warning(f"Received {len(data)}B non-header message: {data[:20]}...")


class ImageConnectionManager(RawConnectionManager, RPCConnectionManager):

    receive_loop = RawConnectionManager.receive_loop

    def get_image_data(self):
        self.start_listening()
        self.send_json({"method": "get_current_img", "id": 0})
        time.sleep(0.1)
        while not self.raw_data:
            logger.debug("Waiting for image data...")
            time.sleep(2)
        self.disconnect()
        return self.raw_data

    def heartbeat_loop(self):
        while self._do_heartbeat:
            if not self.connected:
                try:
                    self.connect()
                except:
                    time.sleep(3)
                    continue
            self.send_json({"method": "test_connection", "id": 1})
            time.sleep(3)


class LogConnectionManager(RawConnectionManager):

    def get_server_log(self):
        self.start_listening()
        self.send_json({"id": 44, "method": "get_server_log"})
        time.sleep(0.1)
        while not self.raw_data:
            logger.debug("Waiting for log message...")
            time.sleep(2)
        self.disconnect()
        return self.raw_data

    def get_user_log(self):
        self.start_listening()
        self.send_json({"id": 5, "method": "get_user_log"})
        time.sleep(0.1)
        while not self.raw_data:
            logger.debug("Waiting for log message...")
            time.sleep(2)
        self.disconnect()
        return self.raw_data

    def heartbeat_loop(self):
        while self._do_heartbeat:
            if not self.connected:
                try:
                    self.connect()
                except:
                    time.sleep(3)
                    continue
            self.send_json({"method": "test_connection", "id": 2})
            time.sleep(3)
