"""Detection thread.

Two backends:

  yolo  -- ultralytics YOLOv8 with model.track(persist=True) so tracking IDs
           stay stable frame to frame. This is the real one.
  mock  -- numpy colour-blob finder, no torch, no model download. Produces
           stable IDs for the coloured rectangles in the generated test clip
           so you can exercise selection, follow commands and the safety
           supervisor on a laptop with nothing installed.

The backend is chosen automatically: yolo if ultralytics imports, mock
otherwise. Force it with --detector.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

from .ingest import Slot
from .state import Track, WorldState

log = logging.getLogger("detect")


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


class MockBackend:
    """Finds saturated colour blobs. Deterministic, ~1 ms/frame, no deps.

    Each colour gets a fixed track ID, which is exactly the property the rest
    of the system depends on -- Unity keys its overlay panels off the ID, and
    the follow controller needs it to survive frame to frame.
    """

    name = "mock"

    # (label, track_id, dominant channel index in BGR, margin)
    TARGETS = [
        ("person", 1, 2, 60),  # red-dominant
        ("chair", 2, 0, 60),  # blue-dominant
    ]

    def infer(self, frame: np.ndarray, frame_id: int) -> list[Track]:
        h, w = frame.shape[:2]
        # Downsample for speed; bbox is scaled back up.
        step = max(1, min(h, w) // 240)
        small = frame[::step, ::step].astype(np.int16)
        out: list[Track] = []

        for label, tid, ch, margin in self.TARGETS:
            others = [c for c in range(3) if c != ch]
            mask = (small[:, :, ch] - small[:, :, others].max(axis=2)) > margin
            if mask.sum() < 30:
                continue
            ys, xs = np.nonzero(mask)
            bbox = (
                float(xs.min() * step),
                float(ys.min() * step),
                float(xs.max() * step),
                float(ys.max() * step),
            )
            out.append(
                Track(
                    id=tid,
                    label=label,
                    bbox=bbox,
                    conf=0.90,
                    frame_id=frame_id,
                )
            )
        return out


class YoloBackend:
    name = "yolo"

    def __init__(self, weights: str = "yolov8n.pt", conf: float = 0.5, imgsz: int = 640):
        from ultralytics import YOLO  # imported lazily

        self.model = YOLO(weights)
        self.conf = conf
        self.imgsz = imgsz

    def infer(self, frame: np.ndarray, frame_id: int) -> list[Track]:
        # track() not predict(): persist=True carries IDs across frames so the
        # VR overlay does not flicker and a selected target stays selected.
        result = self.model.track(
            frame, persist=True, conf=self.conf, imgsz=self.imgsz, verbose=False
        )[0]
        out: list[Track] = []
        if result.boxes is None:
            return out
        for box in result.boxes:
            if box.id is None:
                continue  # untracked detection; no stable identity to select
            out.append(
                Track(
                    id=int(box.id),
                    label=self.model.names[int(box.cls)],
                    bbox=tuple(float(v) for v in box.xyxy[0].tolist()),
                    conf=float(box.conf),
                    frame_id=frame_id,
                )
            )
        return out


def make_backend(kind: str, weights: str, conf: float):
    if kind == "mock":
        return MockBackend()
    try:
        return YoloBackend(weights, conf)
    except Exception as exc:
        if kind == "yolo":
            raise
        log.warning("YOLO unavailable (%s); falling back to mock detector", exc)
        return MockBackend()


# ---------------------------------------------------------------------------
# thread
# ---------------------------------------------------------------------------


class Detector(threading.Thread):
    daemon = True

    def __init__(
        self,
        slot: Slot,
        state: WorldState,
        *,
        kind: str = "auto",
        weights: str = "yolov8n.pt",
        conf: float = 0.5,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="detect")
        self.slot = slot
        self.state = state
        self.stop_event = stop_event or threading.Event()
        self.backend = make_backend(kind, weights, conf)
        self.latency_ms = 0.0
        self.processed = 0

    def run(self) -> None:
        log.info("detector backend: %s", self.backend.name)
        while not self.stop_event.is_set():
            item = self.slot.take()
            if item is None:
                time.sleep(0.002)
                continue

            frame_id, frame = item
            t0 = time.perf_counter()
            try:
                tracks = self.backend.infer(frame, frame_id)
            except Exception:
                log.exception("inference failed on frame %d", frame_id)
                continue
            self.latency_ms = (time.perf_counter() - t0) * 1000.0
            self.processed += 1

            size = (frame.shape[1], frame.shape[0])
            self.state.update_tracks(tracks, frame_id, size)
