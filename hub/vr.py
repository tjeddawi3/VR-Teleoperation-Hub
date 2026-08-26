"""WebRTC endpoint for the headset (and the browser stand-in).

One peer connection carries:
  - a video track (source frames, re-encoded by aiortc)
  - a bidirectional data channel named "link"

The data channel is the whole reason this is WebRTC and not RTSP. Detections
go out on it, selections and heartbeats come back on it, and it shares fate
with the video: if the channel dies, the video died too, and the supervisor
sees the operator link go stale within 500 ms.

Every telemetry payload carries frame_id. The headset should render boxes
against the frame they were computed from, not the newest one -- detections
finish 15-25 ms after their frame arrives, so painting them on live video
makes boxes trail moving objects. See the client for the ring-buffer version.

On encoding cost: aiortc encodes the decoded frames in software here. That is
fine on the A5000 box and convenient for development. To put the Pi's
hardware H.264 bitstream on the wire untouched, replace this track with one
that yields encoded packets and override the sender's encoder -- Luxonis'
EncodedStreamTrack in luxonis/oak-examples is the reference. Worth doing
before you chase the last 20 ms of the latency budget; not worth doing first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from fractions import Fraction
from pathlib import Path

import av
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

from .ingest import Ingest, black_frame
from .state import MODES, WorldState

log = logging.getLogger("vr")

VIDEO_CLOCK = 90000  # Hz, the RTP video clock rate


class SourceVideoTrack(MediaStreamTrack):
    """Publishes the newest decoded frame, paced by arrival."""

    kind = "video"

    def __init__(self, ingest: Ingest) -> None:
        super().__init__()
        self.ingest = ingest
        self._last_id = -1
        self._start = time.monotonic()
        self._placeholder = black_frame()

    async def recv(self) -> av.VideoFrame:
        item = None
        deadline = time.monotonic() + 0.2

        # Wait for a frame we have not sent yet. If ingest stalls, fall
        # through and send the last one again so the connection stays alive
        # and the client keeps rendering rather than freezing on a black gap.
        while time.monotonic() < deadline:
            candidate = self.ingest.render_slot.peek()
            if candidate is not None and candidate[0] != self._last_id:
                item = candidate
                break
            await asyncio.sleep(0.004)

        if item is None:
            item = self.ingest.render_slot.peek() or (self._last_id, self._placeholder)

        frame_id, bgr = item
        self._last_id = frame_id

        frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
        frame.pts = int((time.monotonic() - self._start) * VIDEO_CLOCK)
        frame.time_base = Fraction(1, VIDEO_CLOCK)
        return frame


class VRServer:
    def __init__(
        self,
        state: WorldState,
        ingest: Ingest,
        *,
        client_dir: Path,
        telemetry_hz: float = 30.0,
    ) -> None:
        self.state = state
        self.ingest = ingest
        self.client_dir = client_dir
        self.telemetry_period = 1.0 / telemetry_hz
        self.peers: set[RTCPeerConnection] = set()

    # ---- signaling -----------------------------------------------------

    async def offer(self, request: web.Request) -> web.Response:
        body = await request.json()
        pc = RTCPeerConnection()
        self.peers.add(pc)
        peer_id = id(pc)
        log.info("peer %s connecting", peer_id)

        @pc.on("connectionstatechange")
        async def on_state_change():
            log.info("peer %s: %s", peer_id, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self._drop(pc)

        @pc.on("datachannel")
        def on_datachannel(channel):
            log.info("peer %s: data channel %r open", peer_id, channel.label)
            self.state.mark_vr()

            @channel.on("message")
            def on_message(raw):
                self._handle_client_message(raw)

            asyncio.ensure_future(self._telemetry_loop(channel))

        pc.addTrack(SourceVideoTrack(self.ingest))

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=body["sdp"], type=body["type"])
        )
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return web.json_response(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        )

    async def _drop(self, pc: RTCPeerConnection) -> None:
        self.peers.discard(pc)
        try:
            await pc.close()
        except Exception:
            pass
        if not self.peers:
            # Last operator gone. Do not wait for the watchdog.
            self.state.set_mode("stop", "no operator connected")
            self.state.select(None)

    async def close_all(self) -> None:
        await asyncio.gather(*(pc.close() for pc in list(self.peers)), return_exceptions=True)
        self.peers.clear()

    # ---- operator -> hub ------------------------------------------------

    def _handle_client_message(self, raw: str) -> None:
        self.state.mark_vr()
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        kind = msg.get("type")
        if kind == "heartbeat":
            return

        if kind == "select":
            track_id = msg.get("id")
            # -1 clears, same as null: the headset has no null to send.
            if track_id is None or int(track_id) < 0:
                track_id = None
            self.state.select(int(track_id) if track_id is not None else None)
            log.info("operator selected target %s", track_id)
            return

        if kind == "mode":
            mode = msg.get("mode")
            if mode in MODES:
                self.state.set_mode(mode)
                log.info("operator requested mode %s", mode)
                if mode in ("manual", "stop"):
                    self.state.select(None)
            return

    # ---- hub -> operator ------------------------------------------------

    async def _telemetry_loop(self, channel) -> None:
        try:
            while channel.readyState == "open":
                snap = self.state.snapshot()
                payload = {
                    "type": "world",
                    "t": round(time.time(), 3),
                    "frame_id": snap.frame_id,
                    "frame_w": snap.frame_size[0],
                    "frame_h": snap.frame_size[1],
                    "mode": snap.mode,
                    # -1, never null: JsonUtility parses null as 0, which the
                    # headset would render as a lock on track 0. See PROTOCOL.md.
                    "selected_id": snap.selected_id if snap.selected_id is not None else -1,
                    "detections": [t.as_dict() for t in snap.tracks],
                    "gps": snap.gps,
                    "link": {
                        "pi": snap.pi_connected,
                        "pi_age": round(snap.pi_age, 2) if snap.pi_age < 1e6 else -1.0,
                        "detect_age": round(snap.detect_age, 3)
                        if snap.detect_age < 1e6
                        else -1.0,
                    },
                    "reason": self.state.safety_reason,
                }
                channel.send(json.dumps(payload))
                await asyncio.sleep(self.telemetry_period)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telemetry loop ended")

    # ---- app ------------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/offer", self.offer)
        app.router.add_get("/health", self.health)
        app.router.add_static("/", self.client_dir, show_index=True)
        return app

    async def health(self, request: web.Request) -> web.Response:
        snap = self.state.snapshot()
        return web.json_response(
            {
                "peers": len(self.peers),
                "frame_id": snap.frame_id,
                "ingest_fps": round(self.ingest.fps_estimate, 1),
                "mode": snap.mode,
                "pi": snap.pi_connected,
            }
        )
