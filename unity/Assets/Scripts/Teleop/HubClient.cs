using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Text;
using Unity.WebRTC;
using UnityEngine;
using UnityEngine.Networking;

namespace Teleop
{
    /// <summary>
    /// The operator's end of the link. Negotiates one peer connection with the
    /// hub carrying a receive-only video track and a bidirectional data
    /// channel named "link", then keeps a heartbeat going.
    ///
    /// The heartbeat is a safety mechanism, not a keepalive. It is driven by a
    /// coroutine, which means it runs on Unity's game loop -- so if rendering
    /// hangs, the beat stops, the hub's 500 ms operator watchdog trips, and
    /// the vehicle is commanded to stop. Moving this to a Task or a background
    /// thread would keep the beat alive through a frozen headset and leave the
    /// vehicle driving for an operator who can no longer see anything. Do not
    /// move it.
    ///
    /// Requires com.unity.webrtc (3.0.0-pre.8 or newer). That package's API
    /// has churned across versions; if this does not compile, the offer and
    /// SetLocalDescription calls are where to look first.
    /// </summary>
    [DisallowMultipleComponent]
    public class HubClient : MonoBehaviour
    {
        [Header("Hub")]
        [Tooltip("Base URL of the PC hub, no trailing slash.")]
        public string hubUrl = "http://192.168.1.50:8080";

        [Tooltip("Leave empty on a LAN. Only needed if hub and headset are on " +
                 "different networks, which for a vehicle they should not be.")]
        public string stunUrl = "";

        [Header("Link")]
        [Tooltip("Heartbeats per second. The hub stops the vehicle after 0.5 s " +
                 "of silence, so 5 Hz allows three missed beats.")]
        [Range(2f, 20f)] public float heartbeatHz = 5f;

        [Tooltip("Seconds before retrying a failed connection.")]
        public float reconnectDelay = 2f;

        [Header("Wiring")]
        public VideoSurface surface;

        [Header("Status (read only)")]
        public bool connected;
        public string lastError = "";
        public int telemetryReceived;

        public WorldBuffer World { get; } = new WorldBuffer(32);
        public WorldSnapshot Latest => World.Newest;

        /// <summary>Raised on the main thread for each telemetry frame.</summary>
        public event Action<WorldSnapshot> OnWorld;

        private RTCPeerConnection _pc;
        private RTCDataChannel _link;
        private readonly ConcurrentQueue<string> _inbox = new ConcurrentQueue<string>();
        private Coroutine _heartbeat;

        // ------------------------------------------------------------------

        private void Awake()
        {
            if (surface == null) surface = FindObjectOfType<VideoSurface>();
        }

        private void OnEnable()
        {
            StartCoroutine(WebRTC.Update());   // required by com.unity.webrtc
            StartCoroutine(ConnectLoop());
        }

        private void OnDisable()
        {
            Teardown();
        }

        private void Update()
        {
            // Data channel callbacks arrive off the main thread. Unity API
            // calls from there throw, so everything is queued and drained here.
            while (_inbox.TryDequeue(out var json))
            {
                WorldSnapshot snap;
                try
                {
                    snap = JsonUtility.FromJson<WorldSnapshot>(json);
                }
                catch (Exception e)
                {
                    lastError = "telemetry parse: " + e.Message;
                    continue;
                }

                if (snap == null || snap.type != "world") continue;

                World.Push(snap);
                telemetryReceived++;
                if (surface != null) surface.SetFrameSizeHint(snap.frame_w, snap.frame_h);
                OnWorld?.Invoke(snap);
            }
        }

        // ---- outbound ----------------------------------------------------

        public bool LinkOpen => _link != null && _link.ReadyState == RTCDataChannelState.Open;

        private void Send(string json)
        {
            if (!LinkOpen) return;
            try { _link.Send(json); }
            catch (Exception e) { lastError = "send: " + e.Message; }
        }

        public void SelectTarget(int trackId) => Send(SelectMsg.Json(trackId));
        public void ClearSelection() => Send(SelectMsg.Json(-1));
        public void RequestMode(string mode) => Send(ModeMsg.Json(mode));

        /// <summary>Panic button. Wire this to a physical control, not a menu.</summary>
        public void Stop() => Send(ModeMsg.Json("stop"));

        // ---- connection --------------------------------------------------

        private IEnumerator ConnectLoop()
        {
            while (enabled)
            {
                if (!connected)
                {
                    yield return Negotiate();
                    if (!connected) yield return new WaitForSeconds(reconnectDelay);
                }
                yield return null;
            }
        }

        private IEnumerator Negotiate()
        {
            Teardown();

            var config = new RTCConfiguration();
            if (!string.IsNullOrEmpty(stunUrl))
                config.iceServers = new[] { new RTCIceServer { urls = new[] { stunUrl } } };

            _pc = new RTCPeerConnection(ref config);

            _pc.OnIceConnectionChange = state =>
            {
                if (state == RTCIceConnectionState.Failed ||
                    state == RTCIceConnectionState.Disconnected ||
                    state == RTCIceConnectionState.Closed)
                {
                    connected = false;
                }
            };

            _pc.OnTrack = e =>
            {
                if (e.Track is VideoStreamTrack video)
                {
                    video.OnVideoReceived += tex =>
                    {
                        if (surface != null) surface.SetTexture(tex);
                    };
                }
            };

            _pc.AddTransceiver(TrackKind.Video, new RTCRtpTransceiverInit
            {
                direction = RTCRtpTransceiverDirection.RecvOnly
            });

            _link = _pc.CreateDataChannel("link", new RTCDataChannelInit { ordered = true });
            _link.OnOpen = () =>
            {
                connected = true;
                lastError = "";
                if (_heartbeat == null) _heartbeat = StartCoroutine(Heartbeat());
            };
            _link.OnClose = () => { connected = false; };
            _link.OnMessage = bytes => _inbox.Enqueue(Encoding.UTF8.GetString(bytes));

            var offerOp = _pc.CreateOffer();
            yield return offerOp;
            if (offerOp.IsError) { lastError = "createOffer: " + offerOp.Error.message; yield break; }

            var offer = offerOp.Desc;
            var setLocal = _pc.SetLocalDescription(ref offer);
            yield return setLocal;
            if (setLocal.IsError) { lastError = "setLocal: " + setLocal.Error.message; yield break; }

            // Vanilla ICE: gather everything, then send one offer. Trickle
            // would shave a little setup time but needs a second signaling
            // endpoint, and on a LAN this completes in well under a second.
            float deadline = Time.realtimeSinceStartup + 5f;
            while (_pc.GatheringState != RTCIceGatheringState.Complete &&
                   Time.realtimeSinceStartup < deadline)
                yield return null;

            var body = JsonUtility.ToJson(new SdpPayload
            {
                sdp = _pc.LocalDescription.sdp,
                type = "offer"
            });

            using var req = new UnityWebRequest($"{hubUrl}/offer", "POST")
            {
                uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(body)),
                downloadHandler = new DownloadHandlerBuffer(),
                timeout = 10
            };
            req.SetRequestHeader("Content-Type", "application/json");
            yield return req.SendWebRequest();

            if (req.result != UnityWebRequest.Result.Success)
            {
                lastError = "signaling: " + req.error;
                yield break;
            }

            var reply = JsonUtility.FromJson<SdpPayload>(req.downloadHandler.text);
            var answer = new RTCSessionDescription { type = RTCSdpType.Answer, sdp = reply.sdp };
            var setRemote = _pc.SetRemoteDescription(ref answer);
            yield return setRemote;
            if (setRemote.IsError) lastError = "setRemote: " + setRemote.Error.message;
        }

        private IEnumerator Heartbeat()
        {
            var wait = new WaitForSeconds(1f / Mathf.Max(1f, heartbeatHz));
            while (true)
            {
                if (LinkOpen) Send(HeartbeatMsg.Json());
                yield return wait;
            }
        }

        private void Teardown()
        {
            if (_heartbeat != null) { StopCoroutine(_heartbeat); _heartbeat = null; }
            if (_link != null) { _link.Close(); _link = null; }
            if (_pc != null) { _pc.Close(); _pc.Dispose(); _pc = null; }
            connected = false;
            World.Clear();
        }

        [Serializable]
        private struct SdpPayload
        {
            public string sdp;
            public string type;
        }
    }
}
