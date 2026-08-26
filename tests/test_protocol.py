"""Wire-contract test for the "link" data channel.

Three clients now depend on this payload: client/index.html, the Unity
scripts in unity/Assets/Scripts/Teleop/, and tests/test_e2e.py. C# cannot be
compiled here, so this stands in for that check -- it captures a live
telemetry frame and asserts every field name and type matches what
HubMessages.cs declares.

The failure this exists to catch is silent. JsonUtility does not raise on a
missing or mismatched field; it leaves the C# default. A renamed key becomes
0 or "" in the headset with no error anywhere, and a null selected_id becomes
track ID 0 -- which renders as a permanent lock on whatever object happens to
be ID 0.

Run with the hub up:
    python tools/fake_pi.py &
    python -m hub.main --detector mock &
    python tests/test_protocol.py
"""

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import asyncio
import json
import re

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

CS_PATH = pathlib.Path(__file__).resolve().parent.parent / "unity/Assets/Scripts/Teleop/HubMessages.cs"

# What HubMessages.cs declares. Keep in step with the [Serializable] classes.
SCHEMA = {
    "type": str,
    "t": (int, float),
    "frame_id": int,
    "frame_w": int,
    "frame_h": int,
    "mode": str,
    "selected_id": int,
    "detections": list,
    "gps": dict,
    "link": dict,
    "reason": str,
}
DETECTION_SCHEMA = {"id": int, "label": str, "bbox": list, "conf": (int, float), "frame_id": int}
GPS_SCHEMA = {"lat": (int, float), "lon": (int, float), "alt": (int, float), "fix": int, "hdop": (int, float)}
LINK_SCHEMA = {"pi": bool, "pi_age": (int, float), "detect_age": (int, float)}

failures = []


def check(name, schema, obj):
    for key, want in schema.items():
        if key not in obj:
            failures.append(f"{name}: missing key {key!r}")
            continue
        val = obj[key]
        if val is None:
            failures.append(
                f"{name}.{key} is null -- JsonUtility parses null as 0/\"\" "
                f"with no error. Send a -1 sentinel instead."
            )
            continue
        if not isinstance(val, want):
            failures.append(f"{name}.{key}: got {type(val).__name__}, want {want}")
    extra = set(obj) - set(schema)
    if extra:
        failures.append(f"{name}: undeclared keys {sorted(extra)} (add them to HubMessages.cs)")


def check_no_nulls(node, path="world"):
    """JsonUtility cannot represent null anywhere, not just at the top level."""
    if isinstance(node, dict):
        for k, v in node.items():
            if v is None:
                failures.append(f"{path}.{k} is null")
            else:
                check_no_nulls(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if v is None:
                failures.append(f"{path}[{i}] is null")
            else:
                check_no_nulls(v, f"{path}[{i}]")


def check_cs_fields_declared(payload):
    """Every key on the wire should appear as a field in HubMessages.cs."""
    if not CS_PATH.exists():
        failures.append(f"missing {CS_PATH}")
        return
    source = CS_PATH.read_text()
    declared = set(re.findall(r"public\s+[\w\[\]<>?]+\s+(\w+)\s*;", source))
    for key in payload:
        if key not in declared:
            failures.append(f"wire key {key!r} has no field in HubMessages.cs")
    for key in payload.get("gps", {}):
        if key not in declared:
            failures.append(f"gps.{key} has no field in HubMessages.cs")
    for key in payload.get("link", {}):
        if key not in declared:
            failures.append(f"link.{key} has no field in HubMessages.cs")
    for det in payload.get("detections", [])[:1]:
        for key in det:
            if key not in declared:
                failures.append(f"detections[].{key} has no field in HubMessages.cs")


async def capture() -> dict:
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    link = pc.createDataChannel("link")
    got = asyncio.Event()
    payload = {}

    @link.on("message")
    def _(raw):
        msg = json.loads(raw)
        if msg.get("type") == "world" and not got.is_set():
            payload.update(msg)
            got.set()

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "http://127.0.0.1:8080/offer",
            json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
        ) as r:
            answer = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(**answer))

    await asyncio.wait_for(got.wait(), 10)
    await pc.close()
    return payload


async def main():
    print("capturing a telemetry frame from the hub…")
    payload = await capture()

    check("world", SCHEMA, payload)
    check_no_nulls(payload)
    check_cs_fields_declared(payload)
    check("gps", GPS_SCHEMA, payload.get("gps", {}))
    check("link", LINK_SCHEMA, payload.get("link", {}))
    for i, det in enumerate(payload.get("detections", [])):
        check(f"detections[{i}]", DETECTION_SCHEMA, det)
        if len(det.get("bbox", [])) != 4:
            failures.append(f"detections[{i}].bbox must have 4 elements")

    print(json.dumps(payload, indent=2)[:700] + "\n…")
    if failures:
        print(f"\n{len(failures)} CONTRACT FAILURE(S):")
        for f in failures:
            print("  FAIL ", f)
        sys.exit(1)

    print(f"\nPASS  world payload matches HubMessages.cs "
          f"({len(SCHEMA)} fields, {len(payload['detections'])} detections, no nulls)")
    print("PROTOCOL OK")


asyncio.run(main())
