# Link protocol

The `link` WebRTC data channel, spoken by three clients:

| Client | Location |
| --- | --- |
| Browser console | `client/index.html` |
| Unity APK | `unity/Assets/Scripts/Teleop/` |
| Headless test operator | `tests/test_e2e.py` |

`tests/test_protocol.py` captures a live payload and asserts it matches
`HubMessages.cs`. Run it after changing anything below.

## Rule: no nulls

Unity's `JsonUtility` cannot represent null. It does not raise on one — it
leaves the C# default and carries on. `"selected_id": null` silently becomes
`0` in the headset, which renders as a permanent target lock on whatever
object happens to be track ID 0.

Absent values are therefore **`-1`**, never `null`, at every level of the
payload. This is why the schema differs slightly from what a
JavaScript-only design would use.

## Hub → operator

Sent at 30 Hz on the open channel.

```json
{
  "type": "world",
  "t": 1756089123.482,
  "frame_id": 358,
  "frame_w": 1280,
  "frame_h": 720,
  "mode": "follow",
  "selected_id": 1,
  "detections": [
    { "id": 1, "label": "person", "bbox": [945.0, 294.0, 1071.0, 537.0],
      "conf": 0.9, "frame_id": 358 }
  ],
  "gps": { "lat": 37.7751, "lon": -122.4192, "alt": 12.4, "fix": 4, "hdop": 0.8 },
  "link": { "pi": true, "pi_age": 0.085, "detect_age": 0.031 },
  "reason": ""
}
```

| Field | Notes |
| --- | --- |
| `frame_id` | Source frame counter. The key to lag compensation — see below. |
| `mode` | `manual`, `follow`, `avoid`, `stop`. What is **actually commanded**, after the safety supervisor, not what the operator asked for. |
| `selected_id` | Track ID, or `-1`. The hub is the authority; it can clear a selection the operator never released. |
| `detections[].frame_id` | The frame this box was computed from. Usually equals the top-level value. |
| `gps.fix` | NMEA GGA quality: 0 none, 1 gps, 2 sbas, 4 RTK fixed, 5 RTK float. |
| `link.pi_age` `link.detect_age` | Seconds, or `-1.0` if never received. |
| `reason` | Why the supervisor overrode the requested mode, or `""`. Show this to the operator — a silent downgrade to stop is indistinguishable from a dead link. |

## Operator → hub

```json
{ "type": "heartbeat" }
{ "type": "select", "id": 3 }
{ "type": "mode", "mode": "follow" }
```

`select` with `id: -1` clears the selection. Any message counts as a
heartbeat — the hub stamps the operator link on every inbound message — but
send explicit beats at 5 Hz so a quiet operator still holds the link open.

**The heartbeat is a safety mechanism, not a keepalive.** The hub stops the
vehicle after 500 ms of silence. 5 Hz allows three missed beats. Drive it
from the render loop so a frozen client stops beating; a heartbeat on a
background thread survives a hung headset and leaves the vehicle driving for
an operator who can no longer see.

## Lag compensation

Detection finishes 15–25 ms after the frame it describes, and that frame is
already on its way to the display. Drawing the newest boxes on the newest
video makes them trail moving objects.

Both clients hold a short history of snapshots and draw an older one. They
differ in how they index it:

- `client/index.html` picks by **buffer position**. Correct only while
  telemetry rate equals video frame rate.
- `WorldBuffer.ForDisplay` picks the snapshot whose **`frame_id`** is closest
  to `newest.frame_id - lagFrames`. Correct at any pair of rates — which
  matters as soon as detection runs slower than the camera.

Measure the lag once with the browser slider, then set
`DetectionOverlay.overlayLagFrames` to that number.

If no lag value makes the boxes sit still, the problem is not lag. It is
`VideoSurface.sensorHFovDeg`.

## Changing the protocol

1. Update `hub/vr.py`.
2. Update `HubMessages.cs` (field names must match the JSON keys literally).
3. Update `client/index.html`.
4. Update the schema in `tests/test_protocol.py` and run it.

Step 4 is the one that catches the mistake. Steps 2 and 3 fail silently.
