"""Video ingest.

Reads H.264 from the Pi (udp://) or from a file, decodes to BGR, and drops
each frame into a single-element overwrite slot.

The slot is the point. A queue between decode and inference accumulates lag
whenever the consumer falls behind, and that lag never comes back. An
overwrite slot means the consumer always gets the newest frame and the ones
it could not keep up with are dropped silently -- same reasoning as
cv2.CAP_PROP_BUFFERSIZE=1, but explicit and without OpenCV in the loop.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import av
import numpy as np

log = logging.getLogger("ingest")


class Slot:
    """Single-element overwrite buffer. Newest wins, no backlog."""

    __slots__ = ("_value", "_lock", "_dropped")

    def __init__(self) -> None:
        self._value = None
        self._lock = threading.Lock()
        self._dropped = 0

    def put(self, value) -> None:
        with self._lock:
            if self._value is not None:
                self._dropped += 1
            self._value = value

    def take(self):
        """Return the value and clear the slot, or None if empty."""
        with self._lock:
            value, self._value = self._value, None
        return value

    def peek(self):
        """Return the value without clearing. Used by the video sender."""
        with self._lock:
            return self._value

    @property
    def dropped(self) -> int:
        return self._dropped


class Ingest(threading.Thread):
    """Decode thread.

    Puts (frame_id, bgr_ndarray) into every registered slot. Register more
    than one consumer and each gets the same frame -- the detector takes
    frames, the WebRTC sender peeks at them.
    """

    daemon = True

    def __init__(
        self,
        url: str,
        *,
        live: bool = False,
        loop_file: bool = True,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="ingest")
        self.url = url
        self.live = live
        self.loop_file = loop_file
        self.stop_event = stop_event or threading.Event()

        self.detect_slot = Slot()
        self.render_slot = Slot()

        self.frame_id = 0
        self.size: tuple[int, int] = (0, 0)
        self.fps_estimate = 0.0
        self._fps_window: list[float] = []

    # ------------------------------------------------------------------

    def _open(self):
        if self.live:
            # Low-latency demux: do not buffer or probe, hand packets over
            # as soon as they land.
            options = {
                "fflags": "nobuffer",
                "flags": "low_delay",
                "probesize": "32",
                "analyzeduration": "0",
                "reorder_queue_size": "0",
            }
        else:
            options = {}
        return av.open(self.url, options=options, timeout=5.0)

    def _session(self) -> None:
        container = self._open()
        try:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            wall_start = time.monotonic()

            for packet in container.demux(stream):
                if self.stop_event.is_set():
                    return
                if packet.dts is None:
                    continue

                for frame in packet.decode():
                    if not self.live:
                        # Pace a file at its own timestamps so the rest of
                        # the system sees a realistic frame arrival rate.
                        target = wall_start + float(frame.time or 0.0)
                        delay = target - time.monotonic()
                        if delay > 0:
                            time.sleep(min(delay, 1.0))

                    bgr = frame.to_ndarray(format="bgr24")
                    self.size = (bgr.shape[1], bgr.shape[0])
                    item = (self.frame_id, bgr)
                    self.detect_slot.put(item)
                    self.render_slot.put(item)
                    self.frame_id += 1
                    self._tick_fps()
        finally:
            container.close()

    def _tick_fps(self) -> None:
        now = time.monotonic()
        self._fps_window.append(now)
        if len(self._fps_window) > 60:
            self._fps_window.pop(0)
        if len(self._fps_window) > 2:
            span = self._fps_window[-1] - self._fps_window[0]
            if span > 0:
                self.fps_estimate = (len(self._fps_window) - 1) / span

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                log.info("opening %s (live=%s)", self.url, self.live)
                self._session()
            except Exception:
                log.exception("ingest session failed; retrying in 1s")
                time.sleep(1.0)
                continue

            if self.live:
                log.warning("live stream ended; reopening")
                time.sleep(0.5)
            elif self.loop_file:
                log.info("file ended; looping")
            else:
                log.info("file ended; ingest stopping")
                return


def black_frame(width: int = 1280, height: int = 720) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)
