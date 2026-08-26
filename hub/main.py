"""Hub entry point.

    python -m hub.main --source assets/test_clip.mp4

Threads:
    ingest    decode video -> overwrite slots
    detect    slot -> YOLOv8 -> WorldState

Event loop:
    aiohttp + aiortc   operator link (video out, selections in)
    PiLink             20 Hz command tick out, telemetry in
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import threading
from pathlib import Path

from aiohttp import web

from .detect import Detector
from .ingest import Ingest
from .safety import Limits, Supervisor
from .state import WorldState
from .vehicle import PiLink
from .vr import VRServer

log = logging.getLogger("main")
ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="VR teleoperation hub (PC side)")
    p.add_argument(
        "--source",
        default=str(ROOT / "assets" / "test_clip.mp4"),
        help="video source: a file, or udp://0.0.0.0:5000 for the live Pi stream",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="treat source as a live stream: low-latency demux, no pacing",
    )
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--http-host", default="0.0.0.0")
    p.add_argument("--pi-ws", default="ws://127.0.0.1:8765")
    p.add_argument("--no-pi", action="store_true", help="skip the vehicle link entirely")
    p.add_argument(
        "--detector",
        choices=("auto", "yolo", "mock"),
        default="auto",
        help="auto uses YOLOv8 if ultralytics is importable, else mock",
    )
    p.add_argument("--weights", default="yolov8n.pt")
    p.add_argument("--conf", type=float, default=0.5)
    p.add_argument("--command-hz", type=float, default=20.0)
    p.add_argument("--vr-timeout", type=float, default=0.5)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> None:
    stop_event = threading.Event()
    state = WorldState()

    source = args.source
    live = args.live or source.startswith(("udp://", "rtp://", "rtsp://", "srt://"))
    if not live and not Path(source).exists():
        raise SystemExit(
            f"source not found: {source}\n"
            f"Generate one with:  python tools/make_test_clip.py"
        )

    ingest = Ingest(source, live=live, stop_event=stop_event)
    ingest.start()

    detector = Detector(
        ingest.detect_slot,
        state,
        kind=args.detector,
        weights=args.weights,
        conf=args.conf,
        stop_event=stop_event,
    )
    detector.start()

    supervisor = Supervisor(Limits(vr_timeout=args.vr_timeout))
    vr = VRServer(state, ingest, client_dir=ROOT / "client")

    runner = web.AppRunner(vr.build_app())
    await runner.setup()
    site = web.TCPSite(runner, args.http_host, args.http_port)
    await site.start()
    log.info("operator console on http://localhost:%d/", args.http_port)

    tasks = []
    if not args.no_pi:
        link = PiLink(state, supervisor, url=args.pi_ws, rate_hz=args.command_hz)
        tasks.append(asyncio.create_task(link.run(), name="pi-link"))
    else:
        log.warning("--no-pi: no commands will be sent")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            pass  # Windows

    await shutdown.wait()
    log.info("shutting down")

    stop_event.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await vr.close_all()
    await runner.cleanup()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
