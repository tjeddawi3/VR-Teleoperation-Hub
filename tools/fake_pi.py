"""Stand-in for the vehicle.

Speaks the same WebSocket protocol as VESCCommandPart, runs the same control
laws from the design doc, and prints what the motors would do -- so you can
watch steering and throttle respond to a selection in VR before the VESC is
ever plugged in.

It also checks the things that will hurt you later:
  - sequence numbers arriving in order
  - command rate holding steady
  - the 1 s watchdog actually firing when the hub goes quiet

    python tools/fake_pi.py
    python tools/fake_pi.py --drop-after 20   # simulate a WiFi blackout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import websockets

DEADBAND = 0.05
FOLLOW_DISTANCE_M = 1.0


def follow(cmd: dict, depth_m: float) -> tuple[float, float]:
    """Control law from section 4.2 of the design doc."""
    bbox = cmd["bbox"]
    width = cmd.get("frame_width") or 1920
    cx = (bbox[0] + bbox[2]) / 2
    error = (cx - width / 2) / (width / 2)
    steer = max(-1.0, min(1.0, error * 0.8))
    if abs(steer) < DEADBAND:
        steer = 0.0  # servo slop; small corrections do nothing but jitter
    depth_err = depth_m - FOLLOW_DISTANCE_M
    throttle = 0.3 if depth_err > 0.5 else (0.05 if depth_err < -0.3 else 0.2)
    return steer, throttle


def avoid(cmd: dict) -> tuple[float, float]:
    bbox = cmd["bbox"]
    width = cmd.get("frame_width") or 1920
    cx = (bbox[0] + bbox[2]) / 2
    error = (cx - width / 2) / (width / 2)
    return max(-1.0, min(1.0, -error * 1.0)), 0.2


class FakePi:
    def __init__(self, args) -> None:
        self.args = args
        self.last_seq = 0
        self.count = 0
        self.gaps = 0
        self.out_of_order = 0
        self.started = time.monotonic()
        self.last_cmd_time = time.monotonic()
        self.steering = 0.0
        self.throttle = 0.0
        self.stopped_by_watchdog = False

    def depth_for(self, cmd: dict) -> float:
        """The Pi samples its own 16-bit depth map here.

        Simulated as a slow oscillation between 0.7 and 3.0 m -- the usable
        stereo band for the OAK-D, clamped as the doc recommends.
        """
        phase = (time.monotonic() - self.started) * 0.4
        return 1.85 + 1.15 * math.sin(phase)

    async def handle(self, websocket):
        print(f"hub connected from {websocket.remote_address}")
        asyncio.create_task(self.gps_broadcast(websocket))
        asyncio.create_task(self.watchdog())
        try:
            async for raw in websocket:
                self.on_command(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            print("hub disconnected -> watchdog stop")
            self.steering = self.throttle = 0.0

    def on_command(self, cmd: dict) -> None:
        self.count += 1
        self.last_cmd_time = time.monotonic()
        self.stopped_by_watchdog = False

        seq = cmd.get("seq", 0)
        if seq < self.last_seq:
            self.out_of_order += 1
            return  # stale; discard as the doc specifies
        if seq > self.last_seq + 1 and self.last_seq:
            self.gaps += seq - self.last_seq - 1
        self.last_seq = seq

        mode = cmd.get("mode", "stop")
        if mode == "follow" and cmd.get("bbox"):
            depth = self.depth_for(cmd)
            self.steering, self.throttle = follow(cmd, depth)
            extra = f"depth={depth:4.2f}m"
        elif mode == "avoid" and cmd.get("bbox"):
            self.steering, self.throttle = avoid(cmd)
            extra = ""
        else:
            self.steering, self.throttle = 0.0, 0.0
            extra = cmd.get("reason", "")

        if self.count % self.args.print_every == 0:
            rate = self.count / max(1e-6, time.monotonic() - self.started)
            print(
                f"seq={seq:<6} {mode:<7} "
                f"servo={(self.steering + 1) / 2:5.3f} duty={self.throttle:+5.2f} "
                f"| {rate:4.1f} Hz gaps={self.gaps} stale={self.out_of_order} {extra}"
            )

    async def watchdog(self) -> None:
        """The real one lives in VESCCommandPart._pwm_loop."""
        while True:
            await asyncio.sleep(0.1)
            if time.monotonic() - self.last_cmd_time > 1.0:
                if not self.stopped_by_watchdog:
                    print("!! WATCHDOG: no command in 1.0 s -> SetDutyCycle(0.0)")
                    self.stopped_by_watchdog = True
                self.steering = self.throttle = 0.0

    async def gps_broadcast(self, websocket) -> None:
        t0 = time.monotonic()
        try:
            while True:
                dt = time.monotonic() - t0
                await websocket.send(
                    json.dumps(
                        {
                            "type": "gps",
                            "lat": 37.7749 + 0.00002 * dt,
                            "lon": -122.4194 + 0.00001 * dt,
                            "alt": 12.4,
                            "fix": 4 if dt > 5 else 1,  # RTK acquires after 5 s
                            "hdop": 0.8 if dt > 5 else 3.2,
                        }
                    )
                )
                await asyncio.sleep(0.1)
        except websockets.ConnectionClosed:
            pass


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--print-every", type=int, default=20, help="print 1 in N commands")
    p.add_argument(
        "--drop-after",
        type=float,
        default=0.0,
        help="seconds before killing the connection to test the watchdog",
    )
    args = p.parse_args()

    pi = FakePi(args)
    async with websockets.serve(pi.handle, args.host, args.port):
        print(f"fake Pi listening on ws://{args.host}:{args.port}")
        if args.drop_after:
            await asyncio.sleep(args.drop_after)
            print(f"-- simulating WiFi blackout after {args.drop_after}s --")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
