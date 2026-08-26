"""Link to the Pi.

Outbound: a fixed-rate command tick. Commands are emitted on a clock, never
in response to an operator event. A burst of VR messages cannot flood the
vehicle, and -- more importantly -- silence from the operator does not leave
the last command latched, because the tick keeps running and the supervisor
downgrades it to stop.

Inbound: GPS telemetry and whatever else the Pi broadcasts.

Note on target_depth: the command JSON in the design doc carries a depth in
metres from the PC to the Pi. That value cannot be recovered reliably on this
end -- all the PC sees is a JET-colormapped depth image, and that mapping is
not invertible. The Pi holds the real 16-bit disparity, so this sends the
bbox and lets the Pi sample its own depth map.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from .safety import Supervisor
from .state import WorldState

log = logging.getLogger("vehicle")


class PiLink:
    def __init__(
        self,
        state: WorldState,
        supervisor: Supervisor,
        *,
        url: str = "ws://127.0.0.1:8765",
        rate_hz: float = 20.0,
    ) -> None:
        self.state = state
        self.supervisor = supervisor
        self.url = url
        self.period = 1.0 / rate_hz
        self.sent = 0
        self.last_command: dict = {}

    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, serve, reconnect forever."""
        backoff = 0.5
        while True:
            try:
                async with websockets.connect(
                    self.url, ping_interval=5, ping_timeout=5, close_timeout=1
                ) as ws:
                    log.info("connected to Pi at %s", self.url)
                    self.state.mark_pi(True)
                    backoff = 0.5
                    await asyncio.gather(
                        self._command_tick(ws),
                        self._telemetry(ws),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Pi link down (%s); retry in %.1fs", exc, backoff)
            finally:
                self.state.mark_pi(False)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    # ------------------------------------------------------------------

    async def _command_tick(self, ws) -> None:
        next_at = time.monotonic()
        while True:
            next_at += self.period
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

            snap = self.state.snapshot()
            now = time.time()
            mode, reason = self.supervisor.evaluate(snap, now)
            self.supervisor.log_transition(reason, mode)

            if reason and snap.mode != "stop":
                # Persist the override so the operator UI shows it.
                self.state.set_mode(mode, reason)

            target = next(
                (t for t in snap.tracks if t.id == snap.selected_id), None
            )
            width = snap.frame_size[0] or 1920

            cmd = {
                "mode": mode,
                "target_id": snap.selected_id,
                "target_label": target.label if target else None,
                "bbox": list(target.bbox) if target else None,
                "frame_id": target.frame_id if target else snap.frame_id,
                "frame_width": width,
                "frame_height": snap.frame_size[1] or 1080,
                "seq": self.state.next_seq(),
                "ts": round(now, 4),
                "reason": reason,
            }
            self.last_command = cmd
            await ws.send(json.dumps(cmd))
            self.sent += 1

    async def _telemetry(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("non-JSON from Pi: %r", raw[:80])
                continue

            kind = msg.get("type")
            if kind == "gps":
                self.state.update_gps(
                    {
                        k: msg[k]
                        for k in ("lat", "lon", "alt", "fix", "hdop")
                        if k in msg
                    }
                )
            else:
                self.state.mark_pi(True)
