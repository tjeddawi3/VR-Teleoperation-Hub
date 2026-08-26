using UnityEngine;

namespace Teleop
{
    /// <summary>
    /// Turns a controller or hand ray into a selection message.
    ///
    /// The tie-break matters: when boxes overlap, the smallest one containing
    /// the ray hit wins. A person standing in front of a car sits entirely
    /// inside the car's box, and nearest-hit ordering would give you the car.
    /// The operator almost always means the smaller thing.
    ///
    /// Selection is a request, not a state change. This sends the ID and waits
    /// for the hub to confirm it in telemetry -- the hub is the authority,
    /// because the safety supervisor can clear a selection the operator never
    /// released (target lost, link stale). Rendering local optimistic state
    /// here would show a lock on a target the vehicle has already dropped.
    /// </summary>
    [DisallowMultipleComponent]
    public class TargetSelector : MonoBehaviour
    {
        [Header("Wiring")]
        public HubClient hub;

        [Tooltip("Ray origin. A controller transform, hand ray, or the camera " +
                 "for gaze selection.")]
        public Transform rayOrigin;

        [Header("Input")]
        [Tooltip("Fallback input while testing in the Editor.")]
        public KeyCode editorSelectKey = KeyCode.Mouse0;
        public KeyCode editorClearKey = KeyCode.Mouse1;
        public KeyCode editorStopKey = KeyCode.Space;

        [Header("Ray")]
        public float maxDistance = 50f;
        public LayerMask layers = ~0;

        [Header("Feedback")]
        public bool drawRay = true;
        public Color rayColor = new Color(0.31f, 0.82f, 0.88f, 0.6f);
        public float rayWidth = 0.004f;

        private LineRenderer _ray;
        private readonly RaycastHit[] _hits = new RaycastHit[16];

        private void Awake()
        {
            if (hub == null) hub = FindObjectOfType<HubClient>();
            if (rayOrigin == null && Camera.main != null) rayOrigin = Camera.main.transform;
            BuildRay();
        }

        private void BuildRay()
        {
            _ray = new GameObject("SelectRay").AddComponent<LineRenderer>();
            _ray.transform.SetParent(transform, false);
            _ray.useWorldSpace = true;
            _ray.positionCount = 2;
            _ray.widthMultiplier = rayWidth;
            _ray.material = new Material(Shader.Find("Sprites/Default"));
            _ray.startColor = _ray.endColor = rayColor;
            _ray.enabled = false;
        }

        private void Update()
        {
            if (hub == null || rayOrigin == null) return;

            var ray = new Ray(rayOrigin.position, rayOrigin.forward);
            var target = Probe(ray, out float distance);

            if (drawRay)
            {
                _ray.enabled = true;
                _ray.SetPosition(0, ray.origin);
                _ray.SetPosition(1, ray.origin + ray.direction * distance);
                var c = target != null ? rayColor : new Color(rayColor.r, rayColor.g, rayColor.b, 0.2f);
                _ray.startColor = _ray.endColor = c;
            }
            else
            {
                _ray.enabled = false;
            }

            if (SelectPressed())
            {
                if (target != null) hub.SelectTarget(target.trackId);
                else hub.ClearSelection();   // pointing at nothing means "let go"
            }
            else if (ClearPressed())
            {
                hub.ClearSelection();
            }

            if (StopPressed()) hub.Stop();
        }

        /// <summary>Smallest box containing the ray hit, or null.</summary>
        private DetectionBox Probe(Ray ray, out float distance)
        {
            distance = maxDistance;
            int count = Physics.RaycastNonAlloc(ray, _hits, maxDistance, layers,
                                                QueryTriggerInteraction.Collide);
            DetectionBox best = null;
            float bestArea = float.MaxValue;
            float bestDistance = maxDistance;

            for (int i = 0; i < count; i++)
            {
                var box = _hits[i].collider.GetComponent<DetectionBox>();
                if (box == null || box.trackId < 0) continue;
                if (box.area < bestArea)
                {
                    bestArea = box.area;
                    best = box;
                    bestDistance = _hits[i].distance;
                }
            }

            if (best != null) distance = bestDistance;
            return best;
        }

        // ---- input -------------------------------------------------------
        // Replace these with your XR input bindings. Kept as virtuals so the
        // XR wiring lives in a subclass and this file stays testable in the
        // Editor without a headset attached.

        protected virtual bool SelectPressed() => Input.GetKeyDown(editorSelectKey);
        protected virtual bool ClearPressed() => Input.GetKeyDown(editorClearKey);
        protected virtual bool StopPressed() => Input.GetKeyDown(editorStopKey);
    }
}
