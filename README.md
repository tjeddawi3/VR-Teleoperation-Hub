# VR Teleoperation Hub — PC side

The host-PC layer of the mixed-reality teleoperation system: it ingests the
vehicle's camera stream, runs detection, serves video and detections to the
headset over WebRTC, and drives the vehicle's command channel.

It runs today against a generated video file with no camera, no Pi, and no
GPU — so layers 2, 4, 7, 9, 10 and 11 of the commissioning plan can all be
validated before the motor is ever armed.

## Quickstart

```bash
pip install -r requirements.txt
python tools/make_test_clip.py         # writes assets/test_clip.mp4

python tools/fake_pi.py                # terminal 1 — stands in for the vehicle
python -m hub.main                     # terminal 2 — the hub
```

Open <http://localhost:8080/>, press **Connect**, click a box to select a
target, press **Follow**. Terminal 1 shows the servo and duty-cycle values
the VESC would receive.

Against the real vehicle:

```bash
python -m hub.main --source udp://0.0.0.0:5000 --live --pi-ws ws://<pi-ip>:8765
```

## What runs where

```
ingest thread ── PyAV demux + decode ──┬── detect slot ── YOLOv8 ── WorldState
                                       └── render slot ── WebRTC video track

event loop ───── aiohttp /offer + RTCPeerConnection   (headset)
                 data channel "link"  detections out, selections in
                 PiLink               20 Hz command tick out, GPS in
```

Both consumers read from a **single-element overwrite slot**, not a queue. A
queue between decode and inference accumulates lag whenever the consumer
falls behind and never gives it back. The slot always holds the newest frame
and silently drops what could not be kept up with — the same intent as
`cv2.CAP_PROP_BUFFERSIZE = 1`, made explicit.

| File                      | Role                                                        |
| ------------------------- | ----------------------------------------------------------- |
| `hub/ingest.py`           | Demux, decode, pace, distribute to slots                    |
| `hub/detect.py`           | YOLOv8 `track(persist=True)`, or the dependency-free mock   |
| `hub/state.py`            | `WorldState` — one lock, one writer per field               |
| `hub/safety.py`           | Watchdogs and mode arbitration                              |
| `hub/vehicle.py`          | WebSocket to the Pi, fixed-rate command tick                |
| `hub/vr.py`               | Signaling, video track, bidirectional data channel          |
| `client/index.html`       | Browser operator console — stands in for the APK            |
| `tools/fake_pi.py`        | Vehicle simulator running the doc's control laws            |
| `tools/make_test_clip.py` | Generates a clip with two trackable targets                 |
| `unity/`                  | Headset APK scripts — third client of the same data channel |
| `docs/PROTOCOL.md`        | The `link` wire format all three clients depend on          |

## Three deliberate departures from the design doc

**`target_depth` moved to the Pi.** The doc has the PC sending a depth in
metres. The PC only ever sees a `COLORMAP_JET` image, and that mapping is not
invertible — recovering metres from it is guesswork. The Pi holds the real
16-bit disparity, so the hub sends only the bbox and the Pi samples its own
depth map. The colormap stream stays as an operator visual.

**Commands are emitted on a clock, not on events.** A burst of VR messages
cannot flood the vehicle, and silence from the operator does not leave the
last command latched — the tick keeps running and the supervisor downgrades
it to `stop`.

**A second watchdog on the PC.** The Pi's 1 s watchdog protects against the
PC dying. It does not protect against the PC staying alive and confidently
sending stale commands. `hub/safety.py` catches the operator link going
stale, detections going stale, and the selected target disappearing — and it
commands `stop` rather than going quiet. Nothing in that file can promote a
mode; only an explicit operator action arms the vehicle, and recovery after
an override is never automatic.

## Overlay lag

Detection finishes 15–25 ms after the frame it describes, but that frame is
already on its way to the display. Drawing the newest boxes on the newest
video makes them trail moving objects — this is the "boxes float off objects"
symptom in the doc's known-issues table, and FOV mismatch is only half of it.

The console has an **Overlay lag** slider. Drag it until the boxes sit still
on the target. That number is your measured pipeline depth in frames, and it
is what `VideoReceiver.cs` should use to index its detection ring buffer.

## Video encoding

`aiortc` re-encodes decoded frames in software here. That is fine on the
A5000 and convenient for development, but it is a decode/encode round trip on
the critical path. To put the OAK-D's hardware H.264 on the wire untouched,
replace `SourceVideoTrack` with a track that yields encoded packets and
override the sender's encoder — Luxonis' `EncodedStreamTrack` in
`luxonis/oak-examples` is the reference implementation. Worth doing when you
are chasing the last 20 ms of the 150 ms budget; not worth doing first.

## Tests

```bash
python tests/test_safety.py      # supervisor rules — the layer you cannot bench-test
python tests/test_pipeline.py    # ingest -> detect -> state, no network
python tests/test_e2e.py         # needs fake_pi + hub running; headless operator
python tests/test_protocol.py    # wire format vs. the Unity DTOs
```

`test_protocol.py` exists because C# is not compiled by CI here and
`JsonUtility` fails silently: a renamed key becomes 0 or "" in the headset
with no error anywhere. Run it after touching the payload in `hub/vr.py`.

`test_e2e.py` negotiates a real WebRTC session, selects a target, requests
follow, then stops sending heartbeats and asserts the vehicle is commanded to
stop within the watchdog window.

Simulate a WiFi blackout with `python tools/fake_pi.py --drop-after 20`.

## The headset

`unity/` holds the APK scripts. They speak the same data channel as the
browser console, so the hub has one operator interface, not two. See
[`unity/README.md`](unity/README.md) for scene setup and
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the wire format.

The overlay does not depend on Unity's camera FOV. The video plane and the
detection boxes are sized from a single `sensorHFovDeg` constant and share a
parent transform, so alignment is a 2D mapping that cannot drift — which
removes the doc's "match Unity camera FOV to OAK-D FOV exactly" failure mode
rather than documenting it.

## Detection backends

`--detector auto` uses YOLOv8 if `ultralytics` imports and falls back to a
numpy colour-blob finder otherwise. The mock produces two stable track IDs
against the generated clip, which is enough to exercise selection, the follow
control law, the deadband and every safety rule. Install `ultralytics` and
pass `--detector yolo` when you want the real thing.

## Wiring in the real Pi

`tools/fake_pi.py` speaks the same protocol as `VESCCommandPart`. To swap in
the real vehicle, point `--pi-ws` at it and make one change on the Pi side:
`_handle` should read `bbox` and sample depth locally rather than reading
`target_depth` from the message. Everything else on the wire is unchanged.
