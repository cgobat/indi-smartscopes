
import json
import time
import asyncio
import logging
import threading
from pathlib import Path
from dataclasses import dataclass

import requests
import websockets
import numpy as np

logging.basicConfig(force=True, level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_PORT = 80
DEFAULT_ADDR = "origin.local"
HEARTBEAT_PERIOD = 15
COMMAND_TIMEOUT = 10.

CONFIG_DIR = Path.home() / ".indi_origin"
LOG_DIR = CONFIG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class MountStatus:
    ra_hours: float | None = None
    dec_degrees: float | None = None
    tracking: bool | None = None
    slewing: bool | None = None
    parked: bool | None = None
    altitude_degrees: float | None = None
    azimuth_degrees: float | None = None


@dataclass(slots=True)
class FocuserStatus:
    position: int | None = None
    moving: bool | None = None
    min_position: int | None = None
    max_position: int | None = None


@dataclass(slots=True)
class CameraStatus:
    width: int | None = None
    height: int | None = None
    bin_x: int | None = None
    bin_y: int | None = None
    file_location: str | None = None


class _AsyncRuntime:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro, timeout: float | None = None):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


class OriginProtocol:
    """Async protocol/session client for a single Celestron Origin."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.ws_url = f"ws://{host}/SmartScope-1.0/mountControlEndpoint"
        self.http_base = f"http://{host}/SmartScope-1.0"
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader_task: asyncio.Task | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._latest_by_pair: dict[tuple[str, str], dict[str, object]] = {}
        self._message_handler = None

    async def connect(self, message_handler: Callable = None) -> None:
        if self._ws is not None:
            return
        self._message_handler = message_handler
        self._ws = await websockets.connect(self.ws_url, ping_interval=HEARTBEAT_PERIOD, ping_timeout=HEARTBEAT_PERIOD)
        self._reader_task = asyncio.create_task(self._reader(), name="origin-reader")

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("Origin connection closed"))
        self._pending.clear()

    async def send(self, destination: str, command: str, **payload: object) -> dict[str, object]:
        if self._ws is None:
            raise RuntimeError("Origin transport is not connected")
        self._seq += 1
        seq = self._seq
        body = {
            "Destination": destination,
            "Command": command,
            "Source": "OriginMobileApp",
            "Type": "Command",
            "SequenceID": seq,
            "ExpiredAt": int(time.time() * 1000) + int(COMMAND_TIMEOUT * 1000),
        }
        body.update(payload)
        future = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        await self._ws.send(json.dumps(message))
        return await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT)

    async def wait_for_notification(self, source: str | None = None, command: str | None = None, timeout: float = COMMAND_TIMEOUT) -> dict[str, object]:
        while True:
            message = await asyncio.wait_for(self._notifications.get(), timeout=timeout)
            if source is not None and str(message.get("Source", "")) != source:
                continue
            if command is not None and str(message.get("Command", "")) != command:
                continue
            return message

    async def fetch_image(self, file_location: str, timeout: float = COMMAND_TIMEOUT) -> bytes:
        url = f"{self.http_base}/dev2/{file_location.lstrip('/')}"

        def _fetch() -> bytes:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content

        return await asyncio.to_thread(_fetch)

    async def get_version(self) -> str | None:
        msg = await self.send("System", "GetVersion")
        value = msg.get("Number") or msg.get("Version")
        return str(value) if value is not None else None

    async def get_model(self) -> str | None:
        msg = await self.send("System", "GetModel")
        name = msg.get("Name")
        return str(name) if name is not None else None

    async def refresh_status(self) -> tuple[MountStatus, FocuserStatus, CameraStatus]:
        mount_msg, focuser_msg, camera_info, capture_params = await asyncio.gather(
            self.send("Mount", "GetStatus"),
            self.send("Focuser", "GetStatus"),
            self.send("Camera", "GetCameraInfo"),
            self.send("Camera", "GetCaptureParameters"),
        )
        mount = MountStatus(
            ra_hours=_radians_to_hours(_as_float(mount_msg.get("Enc0"))),
            dec_degrees=np.rad2deg(_as_float(mount_msg.get("Enc1"))),
            tracking=_as_bool(mount_msg.get("IsTracking")),
            slewing=_as_bool(mount_msg.get("IsSlewing")),
            parked=_as_bool(mount_msg.get("IsParked")),
            altitude_degrees=_as_float(mount_msg.get("Altitude")),
            azimuth_degrees=_as_float(mount_msg.get("Azimuth")),
        )
        focuser = FocuserStatus(
            position=_as_int(focuser_msg.get("Position")),
            moving=_as_bool(focuser_msg.get("IsMoving")),
            min_position=_as_int(focuser_msg.get("MinPosition")),
            max_position=_as_int(focuser_msg.get("MaxPosition")),
        )
        camera = CameraStatus(
            width=_as_int(camera_info.get("Width")),
            height=_as_int(camera_info.get("Height")),
            bin_x=_as_int(capture_params.get("BinX")),
            bin_y=_as_int(capture_params.get("BinY")),
            file_location=_as_str(capture_params.get("FileLocation")),
        )
        return mount, focuser, camera

    async def goto_radec(self, ra_hours: float, dec_degrees: float) -> None:
        await self.send("TaskController", "GotoRaDec", Ra=ra_hours, Dec=dec_degrees)

    async def set_tracking(self, enabled: bool) -> None:
        await self.send("Mount", "StartTracking" if enabled else "StopTracking")

    async def slew(self, north: float = 0.0, east: float = 0.0) -> None:
        await self.send("Mount", "Slew", AltRate=north, AzmRate=east)

    async def park(self) -> None:
        await self.send("TaskController", "Park")

    async def move_focuser_absolute(self, position: int, current: int | None = None) -> None:
        if current is None:
            status = await self.send("Focuser", "GetStatus")
            current = _as_int(status.get("Position")) or 0
        delta = int(position) - int(current)
        await self.send("Focuser", "Move", Steps=delta)

    async def capture_preview(self) -> bytes:
        capture = await self.send("Camera", "GetCaptureParameters")
        file_location = _as_str(capture.get("FileLocation"))
        if not file_location:
            message = await self.wait_for_notification(command="NewImageReady", timeout=COMMAND_TIMEOUT)
            file_location = _as_str(message.get("FileLocation"))
        if not file_location:
            raise RuntimeError("Origin did not expose a preview frame path")
        return await self.fetch_image(file_location)

    async def _reader(self) -> None:
        assert self._ws is not None
        async for frame in self._ws:
            if not isinstance(frame, str):
                continue
            try:
                payload = json.loads(frame)
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON frame: %r", frame[:200])
                continue

            source = str(payload.get("Source", ""))
            command = str(payload.get("Command", ""))
            if source and command:
                self._latest_by_pair[(source, command)] = payload

            seq = payload.get("SequenceID")
            if seq is not None:
                try:
                    seq = int(seq)
                except (TypeError, ValueError):
                    seq = None
            if seq is not None and seq in self._pending:
                future = self._pending.pop(seq)
                if not future.done():
                    future.set_result(payload)

            await self._notifications.put(payload)
            if self._message_handler is not None:
                maybe_awaitable = self._message_handler(payload)
                if maybe_awaitable is not None:
                    await maybe_awaitable


class OriginConnectionManager:
    """Synchronous facade used by the pyINDI driver thread model."""

    def __init__(self, address: str, port: int = DEFAULT_PORT) -> None:
        self.address = str(address).strip()
        self.port = int(port)
        self.connected = False
        self._runtime: _AsyncRuntime | None = None
        self._protocol: OriginProtocol | None = None
        self._last_mount = MountStatus()
        self._last_focuser = FocuserStatus()
        self._last_camera = CameraStatus()
        self._model: str | None = None
        self._version: str | None = None

    def _submit(self, coro, timeout: float | None = None):
        if self._runtime is None:
            self._runtime = _AsyncRuntime()
        return self._runtime.submit(coro, timeout=timeout)

    def _ensure_protocol(self) -> OriginProtocol:
        if self._runtime is None:
            self._runtime = _AsyncRuntime()
        if self._protocol is None or self._protocol.host != self.address:
            self._protocol = OriginProtocol(self.address)
        return self._protocol

    def connect(self) -> None:
        if self.connected:
            return
        protocol = self._ensure_protocol()
        self._submit(protocol.connect(), timeout=20.0)
        self._model = self._submit(protocol.get_model(), timeout=COMMAND_TIMEOUT)
        self._version = self._submit(protocol.get_version(), timeout=COMMAND_TIMEOUT)
        self.refresh_status()
        self.connected = True

    def disconnect(self) -> None:
        if self._protocol is not None:
            try:
                self._submit(self._protocol.close(), timeout=10.0)
            except Exception:
                logger.exception("Error during Origin disconnect")
        self.connected = False
        self._protocol = None
        if self._runtime is not None:
            self._runtime.stop()
            self._runtime = None

    def refresh_status(self) -> tuple[MountStatus, FocuserStatus, CameraStatus]:
        protocol = self._ensure_protocol()
        self._last_mount, self._last_focuser, self._last_camera = self._submit(protocol.refresh_status(), timeout=15.0)
        return self._last_mount, self._last_focuser, self._last_camera

    def get_model(self) -> str | None:
        return self._model

    def get_version(self) -> str | None:
        return self._version

    def slew_to_coordinates(self, ra: float, dec: float) -> None:
        protocol = self._ensure_protocol()
        self._submit(protocol.goto_radec(float(ra), float(dec)), timeout=30.0)
        self._last_mount.ra_hours = float(ra)
        self._last_mount.dec_degrees = float(dec)

    def set_tracking(self, enabled: bool) -> None:
        protocol = self._ensure_protocol()
        self._submit(protocol.set_tracking(bool(enabled)), timeout=15.0)
        self._last_mount.tracking = bool(enabled)

    def move_in_direction(self, direction: str, duration: float = 0.5, rate: float = 1.0) -> None:
        protocol = self._ensure_protocol()
        direction = direction.lower()
        north = 0.0
        east = 0.0
        if direction == "north":
            north = rate
        elif direction == "south":
            north = -rate
        elif direction == "east":
            east = rate
        elif direction == "west":
            east = -rate
        else:
            raise ValueError(f"Unsupported direction: {direction}")
        self._submit(protocol.slew(north=north, east=east), timeout=10.0)
        time.sleep(max(0.05, duration))
        self._submit(protocol.slew(north=0.0, east=0.0), timeout=10.0)

    def park(self) -> None:
        protocol = self._ensure_protocol()
        self._submit(protocol.park(), timeout=30.0)
        self._last_mount.parked = True

    def get_focuser_position(self) -> int:
        self.refresh_status()
        return int(self._last_focuser.position or 0)

    def move_focuser_absolute(self, position: int) -> int:
        protocol = self._ensure_protocol()
        current = self.get_focuser_position()
        self._submit(protocol.move_focuser_absolute(int(position), current=current), timeout=30.0)
        self._last_focuser.position = int(position)
        return 0

    def capture_preview(self) -> bytes:
        protocol = self._ensure_protocol()
        return self._submit(protocol.capture_preview(), timeout=20.0)

    def get_state_snapshot(self) -> dict[str, object]:
        mount, focuser, camera = self.refresh_status()
        return {
            "model": self._model,
            "version": self._version,
            "mount": mount,
            "focuser": focuser,
            "camera": camera,
        }


def get_connection_manager(address: str, port: int = DEFAULT_PORT):
    return OriginConnectionManager(address, port)

def _as_float(value: object) -> float | None:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _as_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on", "t"}
    return bool(value)

def _as_str(value: object) -> str | None:
    return str(value) if value is not None else None

def _radians_to_hours(value: float | None) -> float | None:
    if value is None:
        return None
    deg = np.rad2deg(value)
    return deg * 24. / 360.
