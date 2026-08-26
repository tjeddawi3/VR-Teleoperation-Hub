using UnityEngine;

namespace Teleop
{
    /// <summary>
    /// The video background, and the single source of truth for turning
    /// source-frame pixels into world positions.
    ///
    /// The alignment trick: the video quad and every overlay box are sized
    /// from the same sensorHFovDeg constant and live on the same plane, as
    /// children of this transform. Alignment is therefore a pure 2D mapping
    /// that cannot drift, and it does not depend on Unity's camera FOV at all
    /// -- the headset FOV only changes how much of the surface you can see at
    /// once, never where a box sits on it.
    ///
    /// That inverts the usual advice to "match Unity camera FOV to the OAK-D
    /// FOV exactly". You no longer have to, and more importantly you can no
    /// longer get it subtly wrong. The one number that must be right is
    /// sensorHFovDeg, and it is wrong in exactly one visible way: boxes are
    /// uniformly too large or too small, scaling from the frame centre. That
    /// is a much easier bug to see than a gradual drift toward the edges.
    ///
    /// OAK-D RGB (IMX378) is 69 deg horizontal DFOV-corrected; OAK-D Lite
    /// (IMX214) is close but check your unit's datasheet. If you crop or
    /// letterbox on the Pi, this must be the FOV of the cropped image.
    /// </summary>
    [DisallowMultipleComponent]
    public class VideoSurface : MonoBehaviour
    {
        [Header("Sensor")]
        [Tooltip("Horizontal field of view of the SOURCE image, in degrees. " +
                 "If the Pi crops before encoding, use the cropped FOV.")]
        [Range(20f, 140f)]
        public float sensorHFovDeg = 69f;

        [Header("Placement")]
        [Tooltip("Distance from the operator's head to the video plane, metres. " +
                 "Affects comfort only -- not alignment.")]
        [Range(1f, 20f)]
        public float surfaceDistance = 6f;

        [Tooltip("Follow the headset so the surface stays in front of the operator.")]
        public bool lockToHead = true;

        [Tooltip("How quickly the surface catches up to head yaw. 0 = rigid.")]
        [Range(0f, 20f)]
        public float followSharpness = 4f;

        [Header("Debug")]
        public bool drawFrameBorder = false;

        private Transform _head;
        private MeshRenderer _screen;
        private Material _screenMaterial;
        private LineRenderer _border;

        public int FrameWidth { get; private set; } = 1280;
        public int FrameHeight { get; private set; } = 720;

        /// <summary>World-space width of the video plane at surfaceDistance.</summary>
        public float SurfaceWidth =>
            2f * surfaceDistance * Mathf.Tan(sensorHFovDeg * 0.5f * Mathf.Deg2Rad);

        public float SurfaceHeight =>
            SurfaceWidth * (FrameHeight / Mathf.Max(1f, (float)FrameWidth));

        // ------------------------------------------------------------------

        private void Awake()
        {
            _head = Camera.main != null ? Camera.main.transform : null;
            BuildScreen();
        }

        private void BuildScreen()
        {
            var quad = GameObject.CreatePrimitive(PrimitiveType.Quad);
            quad.name = "VideoPlane";
            quad.transform.SetParent(transform, false);
            quad.transform.localPosition = new Vector3(0f, 0f, surfaceDistance);

            // No collider: the video plane must never intercept a selection
            // ray, or every click would land on the background instead of a box.
            Destroy(quad.GetComponent<Collider>());

            _screen = quad.GetComponent<MeshRenderer>();
            _screen.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            _screen.receiveShadows = false;

            // Unlit: this is a camera image, not a lit surface in the scene.
            var shader = Shader.Find("Unlit/Texture") ?? Shader.Find("Sprites/Default");
            _screenMaterial = new Material(shader);
            _screen.material = _screenMaterial;

            _border = new GameObject("FrameBorder").AddComponent<LineRenderer>();
            _border.transform.SetParent(transform, false);
            _border.useWorldSpace = true;
            _border.loop = true;
            _border.positionCount = 4;
            _border.widthMultiplier = 0.01f;
            _border.material = new Material(Shader.Find("Sprites/Default"));
            _border.startColor = _border.endColor = new Color(1f, 1f, 1f, 0.15f);
            _border.enabled = false;
        }

        private void LateUpdate()
        {
            if (lockToHead && _head != null)
            {
                // Yaw only. Pitching the surface with the head makes the
                // operator chase it and is a reliable way to induce nausea.
                var forward = _head.forward;
                forward.y = 0f;
                if (forward.sqrMagnitude > 1e-4f)
                {
                    var target = Quaternion.LookRotation(forward.normalized, Vector3.up);
                    transform.rotation = followSharpness <= 0f
                        ? target
                        : Quaternion.Slerp(transform.rotation, target,
                                           1f - Mathf.Exp(-followSharpness * Time.deltaTime));
                }
                transform.position = _head.position;
            }

            ApplyScale();

            _border.enabled = drawFrameBorder;
            if (drawFrameBorder)
            {
                _border.SetPosition(0, PixelToWorld(0, 0));
                _border.SetPosition(1, PixelToWorld(FrameWidth, 0));
                _border.SetPosition(2, PixelToWorld(FrameWidth, FrameHeight));
                _border.SetPosition(3, PixelToWorld(0, FrameHeight));
            }
        }

        private void ApplyScale()
        {
            if (_screen == null) return;
            var t = _screen.transform;
            t.localPosition = new Vector3(0f, 0f, surfaceDistance);
            t.localScale = new Vector3(SurfaceWidth, SurfaceHeight, 1f);
        }

        // ------------------------------------------------------------------

        /// <summary>Called by HubClient when a decoded video texture arrives.</summary>
        public void SetTexture(Texture texture)
        {
            if (_screenMaterial == null || texture == null) return;
            _screenMaterial.mainTexture = texture;

            if (texture.width > 0 && texture.height > 0)
            {
                FrameWidth = texture.width;
                FrameHeight = texture.height;
            }
        }

        /// <summary>
        /// Telemetry also reports frame size. Prefer the texture when we have
        /// one, but this keeps the projection correct before the first frame
        /// arrives so boxes are never drawn against a guessed aspect ratio.
        /// </summary>
        public void SetFrameSizeHint(int w, int h)
        {
            if (_screenMaterial != null && _screenMaterial.mainTexture != null) return;
            if (w > 0 && h > 0) { FrameWidth = w; FrameHeight = h; }
        }

        /// <summary>
        /// Source pixel (origin top-left, as OpenCV and YOLO report it) to a
        /// world point on the video plane. Everything drawn on top of the
        /// video goes through here.
        /// </summary>
        public Vector3 PixelToWorld(float px, float py)
        {
            float u = px / Mathf.Max(1f, FrameWidth) - 0.5f;      // -0.5 .. +0.5
            float v = 0.5f - py / Mathf.Max(1f, FrameHeight);     // flip: pixels grow down
            return transform.TransformPoint(
                new Vector3(u * SurfaceWidth, v * SurfaceHeight, surfaceDistance));
        }

        /// <summary>World-space size of a pixel rectangle on the plane.</summary>
        public Vector2 PixelSizeToWorld(float pw, float ph)
        {
            return new Vector2(
                pw / Mathf.Max(1f, FrameWidth) * SurfaceWidth,
                ph / Mathf.Max(1f, FrameHeight) * SurfaceHeight);
        }

        public Vector3 PlaneNormal => -transform.forward;
    }
}
