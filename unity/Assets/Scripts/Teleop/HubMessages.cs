using System;

namespace Teleop
{
    /// <summary>
    /// Wire format for the "link" data channel. Field names are snake_case
    /// because JsonUtility matches them literally against the JSON keys --
    /// renaming any of these to C# convention silently zeroes the field.
    ///
    /// Nothing here is nullable. The hub sends -1 for absent values because
    /// JsonUtility parses null as 0, which would make track ID 0 look
    /// permanently selected. See PROTOCOL.md.
    ///
    /// tests/test_protocol.py asserts a live payload matches these types.
    /// If you add a field here, add it there too.
    /// </summary>
    [Serializable]
    public class Detection
    {
        public int id;
        public string label;
        public float[] bbox;    // x1, y1, x2, y2 in source-frame pixels
        public float conf;
        public int frame_id;    // the frame this box was computed from

        public float CenterX => (bbox[0] + bbox[2]) * 0.5f;
        public float CenterY => (bbox[1] + bbox[3]) * 0.5f;
        public float Width => bbox[2] - bbox[0];
        public float Height => bbox[3] - bbox[1];
        public float Area => Width * Height;
    }

    [Serializable]
    public class GpsFix
    {
        public double lat;
        public double lon;
        public float alt;
        public int fix;     // 0 none, 1 gps, 2 sbas, 4 rtk fixed, 5 rtk float
        public float hdop;

        public bool Usable => fix >= 2;

        public string FixName => fix switch
        {
            0 => "no fix",
            1 => "gps",
            2 => "sbas",
            4 => "rtk fixed",
            5 => "rtk float",
            6 => "dead reckoning",
            _ => fix.ToString()
        };
    }

    [Serializable]
    public class LinkHealth
    {
        public bool pi;
        public float pi_age;        // seconds, -1 if never heard from
        public float detect_age;    // seconds, -1 if no detections yet
    }

    /// <summary>One telemetry frame from the hub, at 30 Hz.</summary>
    [Serializable]
    public class WorldSnapshot
    {
        public string type;         // always "world"
        public double t;            // hub wall clock, seconds
        public int frame_id;
        public int frame_w;
        public int frame_h;
        public string mode;         // manual | follow | avoid | stop
        public int selected_id;     // -1 = nothing selected
        public Detection[] detections;
        public GpsFix gps;
        public LinkHealth link;
        public string reason;       // why the supervisor forced a stop, or ""

        public bool HasSelection => selected_id >= 0;

        public Detection Find(int id)
        {
            if (detections == null) return null;
            foreach (var d in detections)
                if (d.id == id) return d;
            return null;
        }
    }

    // ---- outbound -------------------------------------------------------
    // Small enough that JsonUtility.ToJson on a struct is cheaper than
    // string concatenation and less error-prone than hand-built JSON.

    [Serializable]
    public struct HeartbeatMsg
    {
        public string type;
        public static string Json() =>
            UnityEngine.JsonUtility.ToJson(new HeartbeatMsg { type = "heartbeat" });
    }

    [Serializable]
    public struct SelectMsg
    {
        public string type;
        public int id;      // -1 clears the selection
        public static string Json(int id) =>
            UnityEngine.JsonUtility.ToJson(new SelectMsg { type = "select", id = id });
    }

    [Serializable]
    public struct ModeMsg
    {
        public string type;
        public string mode;
        public static string Json(string mode) =>
            UnityEngine.JsonUtility.ToJson(new ModeMsg { type = "mode", mode = mode });
    }
}
