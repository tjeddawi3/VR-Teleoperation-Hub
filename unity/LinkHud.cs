using UnityEngine;

namespace Teleop
{
    /// <summary>
    /// Head-locked status readout: current mode, why the supervisor overrode
    /// it, and the health of each link in the chain.
    ///
    /// The operator cannot see the vehicle, so the only way they know a
    /// command was refused is if this tells them. A silent downgrade to stop
    /// looks identical to a vehicle that is simply not responding, and an
    /// operator who thinks the link is dead does the wrong thing next.
    ///
    /// Deliberately head-locked rather than placed on the video surface: this
    /// has to stay legible precisely when the video has stopped updating.
    /// </summary>
    [DisallowMultipleComponent]
    public class LinkHud : MonoBehaviour
    {
        [Header("Wiring")]
        public HubClient hub;

        [Header("Placement")]
        public float distance = 1.2f;
        public Vector2 offset = new Vector2(0f, -0.42f);  // below centre of view
        public float textSize = 0.02f;

        [Header("Colours")]
        public Color live = new Color(0.50f, 0.79f, 0.54f);
        public Color halt = new Color(0.88f, 0.36f, 0.36f);
        public Color armed = new Color(0.95f, 0.64f, 0.24f);
        public Color dim = new Color(0.42f, 0.48f, 0.55f);

        private Transform _head;
        private TextMesh _mode;
        private TextMesh _detail;

        private void Awake()
        {
            if (hub == null) hub = FindObjectOfType<HubClient>();
            _head = Camera.main != null ? Camera.main.transform : null;
            _mode = MakeText("Mode", 0f, TextAnchor.UpperCenter, 1.5f);
            _detail = MakeText("Detail", -textSize * 2.2f, TextAnchor.UpperCenter, 0.8f);
        }

        private TextMesh MakeText(string name, float y, TextAnchor anchor, float scale)
        {
            var go = new GameObject(name);
            go.transform.SetParent(transform, false);
            go.transform.localPosition = new Vector3(0f, y, 0f);
            var t = go.AddComponent<TextMesh>();
            t.anchor = anchor;
            t.alignment = TextAlignment.Center;
            t.fontSize = 72;
            t.characterSize = textSize * scale;
            return t;
        }

        private void LateUpdate()
        {
            if (_head != null)
            {
                transform.position = _head.position
                                   + _head.forward * distance
                                   + _head.right * offset.x
                                   + _head.up * offset.y;
                transform.rotation = _head.rotation;
            }

            if (hub == null) return;

            if (!hub.connected)
            {
                _mode.text = "NO LINK";
                _mode.color = halt;
                _detail.text = string.IsNullOrEmpty(hub.lastError)
                    ? "connecting to hub…"
                    : hub.lastError;
                _detail.color = dim;
                return;
            }

            var w = hub.Latest;
            if (w == null)
            {
                _mode.text = "WAITING";
                _mode.color = dim;
                _detail.text = "";
                return;
            }

            _mode.text = w.mode.ToUpperInvariant();
            _mode.color = w.mode == "stop" ? halt : armed;

            // The override reason outranks everything else on this display.
            if (!string.IsNullOrEmpty(w.reason))
            {
                _detail.text = w.reason;
                _detail.color = halt;
                return;
            }

            string vehicle = w.link.pi ? "vehicle ok" : "VEHICLE DOWN";
            string detect = w.link.detect_age >= 0f
                ? $"det {Mathf.RoundToInt(w.link.detect_age * 1000f)}ms"
                : "det —";
            string gps = w.gps != null ? w.gps.FixName : "—";
            string target = w.HasSelection ? $"target #{w.selected_id}" : "no target";

            _detail.text = $"{vehicle}   {detect}   gps {gps}   {target}";
            _detail.color = w.link.pi && (w.gps == null || w.gps.Usable) ? live : dim;
        }
    }
}
