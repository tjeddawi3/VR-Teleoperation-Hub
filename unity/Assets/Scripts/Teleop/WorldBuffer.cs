using UnityEngine;

namespace Teleop
{
    /// <summary>
    /// Holds recent telemetry so the overlay can draw boxes against the frame
    /// they were computed from, rather than the newest one.
    ///
    /// Why this exists: detection finishes 15-25 ms after its frame, and that
    /// frame is already travelling to the display. Painting the newest boxes
    /// on the newest video makes them trail moving objects -- the "boxes float
    /// off objects" symptom. FOV mismatch is the other half of that symptom;
    /// this is the half that only shows up when something moves.
    ///
    /// Selection is by frame_id, not by buffer index. The browser console
    /// indexes by position, which only works while telemetry rate happens to
    /// equal video rate. Run detection at 15 Hz against 30 fps video and index
    /// selection silently doubles the compensation. Matching on frame_id is
    /// correct at any pair of rates.
    /// </summary>
    public class WorldBuffer
    {
        private readonly WorldSnapshot[] _ring;
        private int _count;
        private int _head; // next write position

        public WorldBuffer(int capacity = 32)
        {
            _ring = new WorldSnapshot[Mathf.Max(4, capacity)];
        }

        public WorldSnapshot Newest { get; private set; }

        public void Push(WorldSnapshot snap)
        {
            if (snap == null) return;
            _ring[_head] = snap;
            _head = (_head + 1) % _ring.Length;
            if (_count < _ring.Length) _count++;
            Newest = snap;
        }

        public void Clear()
        {
            for (int i = 0; i < _ring.Length; i++) _ring[i] = null;
            _count = 0;
            _head = 0;
            Newest = null;
        }

        /// <summary>
        /// The snapshot to draw right now, given how many source frames the
        /// video pipeline is behind. lagFrames of 0 returns the newest.
        /// </summary>
        public WorldSnapshot ForDisplay(int lagFrames)
        {
            if (_count == 0) return null;
            if (lagFrames <= 0 || _count == 1) return Newest;

            int targetFrame = Newest.frame_id - lagFrames;
            WorldSnapshot best = null;
            int bestDistance = int.MaxValue;

            for (int i = 0; i < _count; i++)
            {
                var candidate = _ring[i];
                if (candidate == null) continue;

                int distance = Mathf.Abs(candidate.frame_id - targetFrame);
                if (distance < bestDistance)
                {
                    bestDistance = distance;
                    best = candidate;
                }
            }

            // If the buffer does not reach back far enough, the oldest entry
            // is the closest we have. Under-compensating is the right failure:
            // boxes lead slightly rather than snapping to a stale position.
            return best ?? Newest;
        }

        /// <summary>Span of frame IDs held, for diagnostics.</summary>
        public int DepthInFrames
        {
            get
            {
                if (_count < 2) return 0;
                int min = int.MaxValue, max = int.MinValue;
                for (int i = 0; i < _count; i++)
                {
                    if (_ring[i] == null) continue;
                    min = Mathf.Min(min, _ring[i].frame_id);
                    max = Mathf.Max(max, _ring[i].frame_id);
                }
                return max - min;
            }
        }
    }
}
