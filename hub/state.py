"""Shared mutable state for the hub.

One lock, coarse-grained. Every field has exactly one writer thread:

    tracks, frame_id      <- detect.Detector
    gps                   <- vehicle.PiLink
    mode, selected_id     <- safety.Supervisor (via vr.py events)
    last_vr_msg           <- vr.py
    last_pi_msg           <- vehicle.PiLink
    seq                   <- vehicle.command_tick

Readers take snapshots. Never hold the lock across an await or an I/O call.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# Operating modes. Mirrors the "mode" field of the command JSON sent to the Pi.
MODES = ("manual", "follow", "avoid", "stop")


@dataclass
class Track:
    """One detected object, in source-frame pixel coordinates."""

    id: int
    label: str
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2
    conf: float
    frame_id: int
    t: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "bbox": [round(v, 1) for v in self.bbox],
            "conf": round(self.conf, 3),
            "frame_id": self.frame_id,
        }


@dataclass
class Snapshot:
    """Immutable read of the world at one instant."""

    tracks: list[Track]
    frame_id: int
    frame_size: tuple[int, int]
    mode: str
    selected_id: Optional[int]
    gps: dict
    detect_age: float
    vr_age: float
    pi_age: float
    pi_connected: bool


class WorldState:
    def __init__(self) -> None:
        self._lock = threading.Lock()

        self.tracks: dict[int, Track] = {}
        self.frame_id: int = 0
        self.frame_size: tuple[int, int] = (0, 0)
        self.last_detect: float = 0.0

        self.gps: dict = {"fix": 0, "lat": 0.0, "lon": 0.0, "alt": 0.0, "hdop": 99.0}

        # Halted by construction. Only an explicit operator action arms it.
        self.mode: str = "stop"
        self.selected_id: Optional[int] = None

        self.last_vr_msg: float = 0.0
        self.last_pi_msg: float = 0.0
        self.pi_connected: bool = False

        self.seq: int = 0

        # Why the supervisor last forced a stop; surfaced to the operator.
        self.safety_reason: str = ""

    # ---- writers -------------------------------------------------------

    def update_tracks(self, tracks: list[Track], frame_id: int, size: tuple[int, int]) -> None:
        with self._lock:
            self.tracks = {t.id: t for t in tracks}
            self.frame_id = frame_id
            self.frame_size = size
            self.last_detect = time.time()

    def update_gps(self, gps: dict) -> None:
        with self._lock:
            self.gps.update(gps)
            self.last_pi_msg = time.time()

    def mark_vr(self) -> None:
        with self._lock:
            self.last_vr_msg = time.time()

    def mark_pi(self, connected: bool) -> None:
        with self._lock:
            self.pi_connected = connected
            if connected:
                self.last_pi_msg = time.time()

    def set_mode(self, mode: str, reason: str = "") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        with self._lock:
            self.mode = mode
            self.safety_reason = reason

    def select(self, track_id: Optional[int]) -> None:
        with self._lock:
            self.selected_id = track_id

    def next_seq(self) -> int:
        with self._lock:
            self.seq += 1
            return self.seq

    # ---- reader --------------------------------------------------------

    def snapshot(self) -> Snapshot:
        now = time.time()
        with self._lock:
            return Snapshot(
                tracks=list(self.tracks.values()),
                frame_id=self.frame_id,
                frame_size=self.frame_size,
                mode=self.mode,
                selected_id=self.selected_id,
                gps=dict(self.gps),
                detect_age=now - self.last_detect if self.last_detect else 1e9,
                vr_age=now - self.last_vr_msg if self.last_vr_msg else 1e9,
                pi_age=now - self.last_pi_msg if self.last_pi_msg else 1e9,
                pi_connected=self.pi_connected,
            )

    def target(self) -> Optional[Track]:
        with self._lock:
            if self.selected_id is None:
                return None
            return self.tracks.get(self.selected_id)
