"""Ingest -> mock detector -> state, no network."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import threading, time, logging
logging.basicConfig(level=logging.INFO)
from hub.ingest import Ingest
from hub.detect import Detector
from hub.state import WorldState

stop = threading.Event()
st = WorldState()
ing = Ingest(str(pathlib.Path(__file__).resolve().parent.parent / "assets" / "test_clip.mp4"), live=False, loop_file=False, stop_event=stop)
ing.start()
det = Detector(ing.detect_slot, st, kind="mock", stop_event=stop)
det.start()

time.sleep(4)
snap = st.snapshot()
print("\n--- after 4s ---")
print("ingest fps      :", round(ing.fps_estimate,1))
print("frames decoded  :", ing.frame_id)
print("frames inferred :", det.processed)
print("dropped (detect):", ing.detect_slot.dropped)
print("infer latency ms:", round(det.latency_ms,2))
print("frame_size      :", snap.frame_size)
for t in snap.tracks:
    print("   track", t.id, t.label, [round(v) for v in t.bbox], "conf", t.conf)
assert len(snap.tracks) == 2, f"expected 2 tracks, got {len(snap.tracks)}"
assert ing.frame_id > 80, ing.frame_id
stop.set()
print("SMOKE OK")
