using System.Collections.Generic;
using UnityEngine;

namespace Teleop
{
    /// <summary>One box in the overlay. Carries the identity the hub uses.</summary>
    public class DetectionBox : MonoBehaviour
    {
        public int trackId = -1;
        public string label = "";
        public float area;      // in source pixels; used to break ray-cast ties
        public bool selected;
    }

    /// <summary>
    /// Draws YOLO detections as world-space rectangles on the video surface.
    ///
    /// Boxes are pooled and generated at runtime -- no prefabs, so this drops
    /// into a project by copying the folder.
    ///
    /// Two settings do real work:
    ///
    /// overlayLagFrames compensates for detections arriving after the frame
    /// they describe. Turn on the browser console's "Overlay lag" slider,
    /// find the value where boxes stop trailing, and put that number here.
    /// If no value works, the problem is sensorHFovDeg, not lag.
    ///
    /// smoothing hides the 30 Hz telemetry rate against a 72-90 Hz display.
    /// It also adds latency, so it is deliberately weak by default. Do not
    /// use it to paper over a wrong overlayLagFrames: smoothing makes a
    /// trailing box trail more smoothly, not less.
    /// </summary>
    [DisallowMultipleComponent]
    public class DetectionOverlay : MonoBehaviour
    {
        [Header("Wiring")]
        public HubClient hub;
        public VideoSurface surface;

        [Header("Lag compensation")]
        [Tooltip("Source frames the video is behind the telemetry. " +
                 "Measure this with the browser console's Overlay lag slider.")]
        [Range(0, 12)]
        public int overlayLagFrames = 2;

        [Header("Appearance")]
        [Range(0f, 1f)] public float smoothing = 0.35f;
        public float lineWidth = 0.012f;
        public Color candidateColor = new Color(0.78f, 0.83f, 0.88f, 0.45f);
        public Color selectedColor = new Color(0.31f, 0.82f, 0.88f, 1f);
        [Tooltip("Corner tick length as a fraction of the shorter box edge.")]
        [Range(0.05f, 0.5f)] public float cornerTickFraction = 0.22f;
        public bool showLabels = true;
        public float labelSize = 0.03f;

        [Header("Filtering")]
        [Range(0f, 1f)] public float minConfidence = 0.35f;
        [Tooltip("Hide everything except the selected target once one is locked.")]
        public bool declutterWhenSelected = false;

        private readonly List<BoxView> _pool = new List<BoxView>();
        private readonly Dictionary<int, Rect> _smoothed = new Dictionary<int, Rect>();
        private readonly HashSet<int> _seenThisFrame = new HashSet<int>();

        // ------------------------------------------------------------------

        private class BoxView
        {
            public GameObject root;
            public LineRenderer outline;   // the rectangle
            public LineRenderer corners;   // lock ticks, selected target only
            public BoxCollider collider;
            public TextMesh label;
            public DetectionBox marker;
        }

        private void Reset()
        {
            hub = FindObjectOfType<HubClient>();
            surface = FindObjectOfType<VideoSurface>();
        }

        private void LateUpdate()
        {
            // LateUpdate, after VideoSurface has settled its transform for the
            // frame. Running in Update would place boxes against last frame's
            // surface orientation and produce a visible one-frame swim.
            if (hub == null || surface == null) { HideFrom(0); return; }

            var snap = hub.World.ForDisplay(overlayLagFrames);
            if (snap == null || snap.detections == null) { HideFrom(0); return; }

            surface.SetFrameSizeHint(snap.frame_w, snap.frame_h);

            _seenThisFrame.Clear();
            int used = 0;

            foreach (var det in snap.detections)
            {
                if (det.bbox == null || det.bbox.Length < 4) continue;
                if (det.conf < minConfidence) continue;

                bool isSelected = snap.HasSelection && det.id == snap.selected_id;
                if (declutterWhenSelected && snap.HasSelection && !isSelected) continue;

                _seenThisFrame.Add(det.id);
                var rect = Smooth(det);
                Render(GetView(used++), det, rect, isSelected);
            }

            HideFrom(used);
            PruneSmoothing();
        }

        // ---- smoothing ---------------------------------------------------

        private Rect Smooth(Detection det)
        {
            var target = new Rect(det.bbox[0], det.bbox[1],
                                  det.bbox[2] - det.bbox[0],
                                  det.bbox[3] - det.bbox[1]);

            if (smoothing <= 0f) { _smoothed[det.id] = target; return target; }

            if (!_smoothed.TryGetValue(det.id, out var current))
            {
                // First sight of this ID: snap. Easing in from a default
                // position would make every new detection fly across the view.
                _smoothed[det.id] = target;
                return target;
            }

            // Frame-rate independent exponential approach.
            float k = 1f - Mathf.Exp(-(1f - smoothing) * 30f * Time.deltaTime);
            var next = new Rect(
                Mathf.Lerp(current.x, target.x, k),
                Mathf.Lerp(current.y, target.y, k),
                Mathf.Lerp(current.width, target.width, k),
                Mathf.Lerp(current.height, target.height, k));
            _smoothed[det.id] = next;
            return next;
        }

        private void PruneSmoothing()
        {
            if (_smoothed.Count <= _seenThisFrame.Count) return;
            var stale = new List<int>();
            foreach (var id in _smoothed.Keys)
                if (!_seenThisFrame.Contains(id)) stale.Add(id);
            foreach (var id in stale) _smoothed.Remove(id);
        }

        // ---- drawing -----------------------------------------------------

        private void Render(BoxView view, Detection det, Rect rect, bool isSelected)
        {
            view.root.SetActive(true);

            Vector3 tl = surface.PixelToWorld(rect.xMin, rect.yMin);
            Vector3 tr = surface.PixelToWorld(rect.xMax, rect.yMin);
            Vector3 br = surface.PixelToWorld(rect.xMax, rect.yMax);
            Vector3 bl = surface.PixelToWorld(rect.xMin, rect.yMax);

            var color = isSelected ? selectedColor : candidateColor;
            view.outline.startColor = view.outline.endColor = color;
            view.outline.widthMultiplier = isSelected ? lineWidth * 1.8f : lineWidth;
            view.outline.SetPosition(0, tl);
            view.outline.SetPosition(1, tr);
            view.outline.SetPosition(2, br);
            view.outline.SetPosition(3, bl);

            // Corner ticks read as a target lock; a second colour would just
            // add noise, so the selected state is carried by weight and shape.
            view.corners.enabled = isSelected;
            if (isSelected)
            {
                float t = cornerTickFraction;
                view.corners.startColor = view.corners.endColor = color;
                view.corners.widthMultiplier = lineWidth * 2.4f;
                view.corners.positionCount = 12;
                SetCorner(view.corners, 0, tl, tr, bl, t);
                SetCorner(view.corners, 3, tr, tl, br, t);
                SetCorner(view.corners, 6, br, bl, tr, t);
                SetCorner(view.corners, 9, bl, br, tl, t);
            }

            // Collider spans the box on the surface plane, thin in depth so it
            // cannot be hit from behind or swallow neighbouring boxes.
            var size = surface.PixelSizeToWorld(rect.width, rect.height);
            view.collider.transform.position = (tl + br) * 0.5f;
            view.collider.transform.rotation = surface.transform.rotation;
            view.collider.size = new Vector3(Mathf.Abs(size.x), Mathf.Abs(size.y), 0.02f);

            view.marker.trackId = det.id;
            view.marker.label = det.label;
            view.marker.area = det.Area;
            view.marker.selected = isSelected;

            view.label.gameObject.SetActive(showLabels);
            if (showLabels)
            {
                view.label.text = isSelected
                    ? $"▸ {det.label} #{det.id}"
                    : $"{det.label} #{det.id}  {Mathf.RoundToInt(det.conf * 100f)}%";
                view.label.color = color;
                view.label.transform.position = tl + surface.transform.up * (labelSize * 0.9f);
                view.label.transform.rotation = surface.transform.rotation;
                view.label.characterSize = labelSize;
            }
        }

        private static void SetCorner(LineRenderer lr, int i, Vector3 corner,
                                      Vector3 alongA, Vector3 alongB, float t)
        {
            lr.SetPosition(i, Vector3.Lerp(corner, alongA, t));
            lr.SetPosition(i + 1, corner);
            lr.SetPosition(i + 2, Vector3.Lerp(corner, alongB, t));
        }

        // ---- pool --------------------------------------------------------

        private BoxView GetView(int index)
        {
            while (_pool.Count <= index) _pool.Add(CreateView(_pool.Count));
            return _pool[index];
        }

        private void HideFrom(int index)
        {
            for (int i = index; i < _pool.Count; i++)
            {
                if (_pool[i].root.activeSelf)
                {
                    _pool[i].root.SetActive(false);
                    _pool[i].marker.trackId = -1;
                }
            }
        }

        private BoxView CreateView(int index)
        {
            var root = new GameObject($"Box_{index}");
            root.transform.SetParent(transform, false);

            var mat = new Material(Shader.Find("Sprites/Default"));

            var outline = new GameObject("Outline").AddComponent<LineRenderer>();
            outline.transform.SetParent(root.transform, false);
            outline.useWorldSpace = true;
            outline.loop = true;
            outline.positionCount = 4;
            outline.material = mat;
            outline.numCornerVertices = 2;
            outline.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;

            var corners = new GameObject("Corners").AddComponent<LineRenderer>();
            corners.transform.SetParent(root.transform, false);
            corners.useWorldSpace = true;
            corners.loop = false;
            corners.positionCount = 12;
            corners.material = mat;
            corners.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            corners.enabled = false;

            var colliderObj = new GameObject("Hit");
            colliderObj.transform.SetParent(root.transform, false);
            var box = colliderObj.AddComponent<BoxCollider>();
            box.isTrigger = true;
            var marker = colliderObj.AddComponent<DetectionBox>();

            var labelObj = new GameObject("Label");
            labelObj.transform.SetParent(root.transform, false);
            var text = labelObj.AddComponent<TextMesh>();
            text.anchor = TextAnchor.LowerLeft;
            text.fontSize = 72;          // large font, small characterSize = crisp
            text.characterSize = labelSize;

            root.SetActive(false);
            return new BoxView
            {
                root = root, outline = outline, corners = corners,
                collider = box, label = text, marker = marker
            };
        }
    }
}
