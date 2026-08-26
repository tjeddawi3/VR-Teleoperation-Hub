"""Generate a test clip so the hub runs with nothing plugged in.

Two coloured rectangles move across a frame: a red one on a slow sine (the
"person"), a blue one drifting the other way (the "chair"). The mock detector
locks onto both, which gives you two selectable targets and a steering error
that swings through zero -- enough to exercise selection, the follow control
law, the deadband and the safety supervisor.

    python tools/make_test_clip.py
    python tools/make_test_clip.py --seconds 60 --width 1920 --height 1080
"""

from __future__ import annotations

import argparse
import math
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def draw_rect(frame: np.ndarray, cx: int, cy: int, w: int, h: int, bgr) -> None:
    y0, y1 = max(0, cy - h // 2), min(frame.shape[0], cy + h // 2)
    x0, x1 = max(0, cx - w // 2), min(frame.shape[1], cx + w // 2)
    if y1 > y0 and x1 > x0:
        frame[y0:y1, x0:x1] = bgr


def build(path: Path, width: int, height: int, fps: int, seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(path), mode="w")
    stream = container.add_stream("libx264", rate=fps)
    stream.width, stream.height = width, height
    stream.pix_fmt = "yuv420p"
    stream.time_base = Fraction(1, fps)
    # Short GOP: a viewer joining mid-stream sees video within ~1 s, and the
    # decoder recovers fast after packet loss. Same setting you want on the
    # OAK's VideoEncoder.
    stream.options = {
        "preset": "veryfast",
        "tune": "zerolatency",
        "g": str(fps),
        "crf": "23",
    }

    total = fps * seconds
    # Static background gradient so the frame is not flat grey.
    ramp = np.linspace(40, 90, height, dtype=np.uint8)[:, None]
    background = np.repeat(ramp[:, :, None], 3, axis=2)
    background = np.repeat(background, width, axis=1)

    for i in range(total):
        t = i / fps
        frame = background.copy()

        # Horizon line, for a sense of motion.
        offset = int((t * 60) % 80)
        for x in range(-offset, width, 80):
            if 0 <= x < width:
                frame[height // 2 : height // 2 + 3, max(0, x) : x + 40] = (70, 70, 70)

        # "person": red, sweeps across the full width
        px = int(width / 2 + (width * 0.34) * math.sin(t * 0.55))
        py = int(height * 0.58)
        ph = int(height * 0.34)
        draw_rect(frame, px, py, int(ph * 0.42), ph, (40, 40, 225))

        # "chair": blue, drifts the other way, slower
        cx = int(width / 2 - (width * 0.22) * math.sin(t * 0.33 + 1.2))
        cy = int(height * 0.66)
        ch = int(height * 0.18)
        draw_rect(frame, cx, cy, int(ch * 1.1), ch, (220, 120, 40))

        video_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts = i
        video_frame.time_base = Fraction(1, fps)
        for packet in stream.encode(video_frame):
            container.mux(packet)

    for packet in stream.encode():
        container.mux(packet)
    container.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=str(ROOT / "assets" / "test_clip.mp4"))
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--seconds", type=int, default=30)
    args = p.parse_args()

    out = Path(args.out)
    build(out, args.width, args.height, args.fps, args.seconds)
    size_mb = out.stat().st_size / 1e6
    print(f"wrote {out} ({args.width}x{args.height} @ {args.fps}fps, {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
