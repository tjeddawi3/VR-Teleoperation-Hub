"""Safety supervisor unit tests -- the layer you cannot test on a bench."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import time
from hub.safety import Supervisor, Limits
from hub.state import Snapshot, Track

def snap(mode="follow", vr=0.05, det=0.05, sel=1, ids=(1,2), pi=True):
    return Snapshot(
        tracks=[Track(i, "person", (100,100,200,300), .9, 7) for i in ids],
        frame_id=7, frame_size=(1280,720), mode=mode, selected_id=sel,
        gps={"fix":4}, detect_age=det, vr_age=vr, pi_age=0.05, pi_connected=pi)

now = time.time()
s = Supervisor(Limits())

cases = [
    ("healthy follow stays follow",      snap(),                              "follow", False),
    ("operator link stale -> stop",      snap(vr=0.9),                        "stop",   True),
    ("no operator at all -> stop",       snap(vr=1e9),                        "stop",   True),
    ("stale detections -> stop",         snap(det=0.9),                       "stop",   True),
    ("nothing selected -> stop",         snap(sel=None),                      "stop",   True),
    ("manual ignores detection age",     snap(mode="manual", det=99, sel=None),"manual", False),
    ("explicit stop is honoured",        snap(mode="stop", vr=1e9),           "stop",   False),
]
for name, sn, want_mode, want_reason in cases:
    s2 = Supervisor(Limits())
    mode, reason = s2.evaluate(sn, now)
    ok = mode == want_mode and bool(reason) == want_reason
    print(f"{'PASS' if ok else 'FAIL'}  {name:36} -> {mode:6} {reason!r}")
    assert ok, name

# Target-loss grace: one dropped detection must not stutter the vehicle,
# but a sustained loss must stop it.
s3 = Supervisor(Limits(target_grace=0.3))
m, r = s3.evaluate(snap(ids=(2,)), now)
assert m == "follow" and r == "", (m, r)
print("PASS  target briefly lost -> holds within grace")
m, r = s3.evaluate(snap(ids=(2,)), now + 0.35)
assert m == "stop" and "lost" in r, (m, r)
print("PASS  target lost past grace           -> stop", repr(r))

# Recovery must be operator-initiated, never automatic.
s4 = Supervisor(Limits())
s4.evaluate(snap(vr=9.0), now)
m, _ = s4.evaluate(snap(mode="stop", vr=0.05), now + 1)
assert m == "stop"
print("PASS  no auto-resume after link recovers")
print("\nALL SAFETY TESTS PASSED")
