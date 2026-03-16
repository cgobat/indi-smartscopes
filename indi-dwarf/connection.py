from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

logging.basicConfig(force=True, level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONTROL_PORT = 9900
HTTP_PORT = 8082
JPEG_PORT = 8092
DEFAULT_ADDR = "192.168.88.1"
GUIDER_PORT = 0
IMAGING_PORT = JPEG_PORT
LOGGING_PORT = 0

CONFIG_DIR = Path.home() / ".indi_dwarf"
LOG_DIR = CONFIG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class _AsyncLoopThread:
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


class DwarfConnectionManager:
    """Thin pyINDI-facing bridge over dwarf_alpaca.dwarf.session.DwarfSession."""

    def __init__(self, address: str, port: int = CONTROL_PORT, *, device_model: str = "dwarf3") -> None:
        self.address = str(address).strip()
        self.port = int(port)
        self.device_model = device_model
        self.connected = False
        self.filter_names = ["VIS", "Astro", "Duo-band"]
        self._loop_thread: _AsyncLoopThread | None = None
        self._session = None
        self._latest_ra: float = 0.0
        self._latest_dec: float = 0.0

    @property
    def destination(self) -> str:
        return f"{self.address}:{self.port}"

    def _ensure_runtime(self) -> None:
        if self._loop_thread is None:
            self._loop_thread = _AsyncLoopThread()
        if self._session is None:
            self._session = self._submit(self._create_session())

    def _submit(self, coro, timeout: float | None = None):
        if self._loop_thread is None:
            self._loop_thread = _AsyncLoopThread()
        return self._loop_thread.submit(coro, timeout=timeout)

    async def _create_session(self):
        from dwarf_alpaca.config.settings import Settings
        from dwarf_alpaca.dwarf.session import DwarfSession

        settings = Settings(
            dwarf_ap_ip=self.address,
            dwarf_ws_port=self.port,
            dwarf_http_port=HTTP_PORT,
            dwarf_jpeg_port=JPEG_PORT,
            dwarf_device_model=self.device_model,
        )
        return DwarfSession(settings)

    def connect(self) -> None:
        if self.connected:
            return
        self._ensure_runtime()
        assert self._session is not None
        for device_name in ("telescope", "camera", "focuser", "filterwheel"):
            self._submit(self._session.acquire(device_name), timeout=20.0)
        self._submit(self._session.camera_connect(), timeout=20.0)
        self._submit(self._session.focuser_connect(), timeout=20.0)
        try:
            self.filter_names = self._submit(self._session.get_filter_labels(), timeout=10.0)
        except Exception:
            logger.exception("Failed to load filter labels; using defaults")
        self.connected = True

    def disconnect(self) -> None:
        if self._session is not None and self.connected:
            try:
                self._submit(self._session.camera_disconnect(), timeout=10.0)
            except Exception:
                logger.exception("camera_disconnect failed")
            try:
                self._submit(self._session.focuser_disconnect(), timeout=10.0)
            except Exception:
                logger.exception("focuser_disconnect failed")
            for device_name in ("filterwheel", "focuser", "camera", "telescope"):
                try:
                    self._submit(self._session.release(device_name), timeout=10.0)
                except Exception:
                    logger.exception("release failed for %s", device_name)
            try:
                self._submit(self._session.shutdown(), timeout=10.0)
            except Exception:
                logger.exception("shutdown failed")
        self.connected = False
        self._session = None
        if self._loop_thread is not None:
            self._loop_thread.stop()
            self._loop_thread = None

    def sync_clock(self) -> None:
        self._ensure_runtime()
        self._submit(self._session._sync_device_clock(), timeout=10.0)

    def slew_to_coordinates(self, ra: float, dec: float, target_name: str = "INDI Target") -> tuple[float, float]:
        self._ensure_runtime()
        self._submit(
            self._session.telescope_slew_to_coordinates(float(ra), float(dec), target_name=target_name),
            timeout=180.0,
        )
        self._latest_ra = float(ra)
        self._latest_dec = float(dec)
        return self._latest_ra, self._latest_dec

    def abort_slew(self) -> None:
        self._ensure_runtime()
        self._submit(self._session.telescope_abort_slew(), timeout=15.0)

    def move_in_direction(self, direction: str, duration: float = 0.5) -> None:
        self._ensure_runtime()
        direction = direction.lower()
        mapping = {
            "east": (0, 1.0),
            "west": (0, -1.0),
            "north": (1, 1.0),
            "south": (1, -1.0),
        }
        axis, rate = mapping[direction]
        self._submit(self._session.telescope_move_axis(axis, rate), timeout=10.0)
        time.sleep(max(0.05, duration))
        self._submit(self._session.telescope_stop_axis(axis), timeout=10.0)

    def get_equatorial_coordinates(self) -> tuple[float, float]:
        return self._latest_ra, self._latest_dec

    def start_exposure(self, duration_sec: float = 1.0, light: bool = True) -> None:
        self._ensure_runtime()
        duration_sec = float(duration_sec)
        self._submit(
            self._session.camera_start_exposure(duration_sec, bool(light)),
            timeout=max(30.0, duration_sec + 20.0),
        )

    def read_exposure(self, timeout: float | None = None, poll_interval: float = 0.2):
        self._ensure_runtime()
        deadline = time.time() + (max(30.0, float(timeout)) if timeout is not None else 60.0)
        while time.time() < deadline:
            image = self._submit(self._session.camera_readout(), timeout=10.0)
            if image is not None:
                return image
            time.sleep(max(0.05, float(poll_interval)))
        raise TimeoutError("Timed out waiting for DWARF exposure readout")

    def set_binning(self, bin_val: int) -> None:
        self._ensure_runtime()
        state = self._session.camera_state
        state.requested_bin = (int(bin_val), int(bin_val))

    def get_filter_labels(self) -> list[str]:
        self._ensure_runtime()
        self.filter_names = self._submit(self._session.get_filter_labels(), timeout=10.0)
        return self.filter_names

    def get_filter_position(self) -> int:
        self._ensure_runtime()
        state = self._session.camera_state
        return int(state.filter_index or 0)

    def set_filter_position(self, pos: int) -> str:
        self._ensure_runtime()
        return str(self._submit(self._session.set_filter_position(int(pos)), timeout=20.0))

    def get_focuser_position(self) -> int:
        self._ensure_runtime()
        return int(self._session.focuser_state.position)

    def move_focuser_absolute(self, position: int) -> int:
        self._ensure_runtime()
        current = int(self._session.focuser_state.position)
        delta = int(position) - current
        self._submit(self._session.focuser_move(delta, target=int(position)), timeout=60.0)
        return 0

    def get_camera_temperature(self) -> float | None:
        self._ensure_runtime()
        temp = getattr(self._session.camera_state, "temperature_c", None)
        return None if temp is None else float(temp)

    def get_state_snapshot(self) -> dict[str]:
        self._ensure_runtime()
        session = self._session
        assert session is not None
        camera_state = self._session.camera_state
        focuser_state = self._session.focuser_state
        telescope_state = self._session.telescope_state

        state_dict = {
            "device_model": self.device_model,
            "destination": self.destination,
            "ra_hours": self._latest_ra,
            "dec_degs": self._latest_dec,
            "camera_state": camera_state,
            "focuser_state": focuser_state,
            "telescope_state": telescope_state,
        }

        return state_dict
