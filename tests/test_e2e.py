"""Headless operator: proves the WebRTC path and the full command loop.

Stands in for the Unity APK -- negotiates video + data channel, selects a
target, requests follow, and confirms the fake Pi produces real steering.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import asyncio, json, aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription

async def main():
    pc = RTCPeerConnection()
    pc.addTransceiver("video", direction="recvonly")
    link = pc.createDataChannel("link")
    frames = {"n": 0}
    worlds = []

    @pc.on("track")
    def on_track(track):
        async def drain():
            while True:
                await track.recv()
                frames["n"] += 1
        asyncio.ensure_future(drain())

    ready = asyncio.Event()
    @link.on("open")
    def _(): ready.set()
    @link.on("message")
    def _(raw):
        w = json.loads(raw)
        if w.get("type") == "world":
            worlds.append(w)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    async with aiohttp.ClientSession() as s:
        async with s.post("http://127.0.0.1:8080/offer", json={
            "sdp": pc.localDescription.sdp, "type": pc.localDescription.type}) as r:
            ans = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(**ans))

    await asyncio.wait_for(ready.wait(), 10)
    print("PASS  data channel open")

    async def heartbeat():
        while True:
            link.send(json.dumps({"type": "heartbeat"})); await asyncio.sleep(0.2)
    hb = asyncio.ensure_future(heartbeat())

    await asyncio.sleep(2.0)
    assert worlds, "no telemetry received"
    w = worlds[-1]
    print(f"PASS  telemetry: {len(worlds)} msgs, {len(w['detections'])} detections, frame {w['frame_id']}")
    assert frames["n"] > 10, f"only {frames['n']} video frames"
    print(f"PASS  video: {frames['n']} frames decoded over WebRTC")

    target = w["detections"][0]
    link.send(json.dumps({"type": "select", "id": target["id"]}))
    await asyncio.sleep(0.3)
    link.send(json.dumps({"type": "mode", "mode": "follow"}))
    await asyncio.sleep(2.0)
    w = worlds[-1]
    print(f"PASS  mode is now {w['mode']!r}, selected {w['selected_id']}, reason {w['reason']!r}")
    assert w["mode"] == "follow", w

    # Stop the heartbeat: the operator watchdog must trip within ~500 ms.
    hb.cancel()
    await asyncio.sleep(1.5)
    w = worlds[-1]
    print(f"PASS  heartbeat stopped -> mode {w['mode']!r}, reason {w['reason']!r}")
    assert w["mode"] == "stop" and w["reason"], w
    await pc.close()
    print("\nE2E OK")

asyncio.run(main())
