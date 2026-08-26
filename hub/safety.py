"""Safety supervision.

The Pi already stops the vehicle after 1 s of silence from the PC. That
protects against the PC dying. It does not protect against the PC staying
alive and confidently sending stale commands -- a hung render thread, a
headset that walked out of WiFi range, a target that left the frame. Those
are the failures this module catches, and it catches them by commanding
"stop" rather than by going quiet.

Every rule here downgrades authority. Nothing in this file can promote a mode
to follow or avoid; only an explicit operator action can do that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .state import Snapshot

log = logging.getLogger("safety")


@dataclass
class Limits:
    # Operator link. Headset heartbeats at 5 Hz, so 0.5 s is ~3 missed beats.
    vr_timeout: float = 0.5
    # Detections feeding an autonomous mode must be fresh.
    detect_timeout: float = 0.4
    # Grace period after a selected target disappears before stopping.
    # Short enough to matter, long enough to ride out one missed detection.
    target_grace: float = 0.3


class Supervisor:
    """Decides the mode that is actually safe to command right now."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        self._target_lost_since: float | None = None
        self._last_reason = ""

    def evaluate(self, snap: Snapshot, now: float) -> tuple[str, str]:
        """Return (mode, reason). Reason is empty when nothing was overridden."""
        lim = self.limits
        requested = snap.mode

        if requested == "stop":
            return "stop", ""

        if snap.vr_age > lim.vr_timeout:
            if snap.vr_age > 1e6:
                return "stop", "no operator connected"
            return "stop", f"operator link stale ({snap.vr_age:.1f}s)"

        if requested == "manual":
            # Manual is direct operator input; it does not depend on detections.
            self._target_lost_since = None
            return "manual", ""

        # follow / avoid from here down -- both need a live target.
        if snap.detect_age > lim.detect_timeout:
            return "stop", f"detections stale ({snap.detect_age:.1f}s)"

        if snap.selected_id is None:
            return "stop", "no target selected"

        present = any(t.id == snap.selected_id for t in snap.tracks)
        if present:
            self._target_lost_since = None
        else:
            if self._target_lost_since is None:
                self._target_lost_since = now
            lost_for = now - self._target_lost_since
            if lost_for > lim.target_grace:
                return "stop", f"target {snap.selected_id} lost"
            # Inside the grace window: hold the last command rather than
            # stuttering the vehicle on a single dropped detection.

        return requested, ""

    def log_transition(self, reason: str, mode: str = "") -> None:
        if reason == self._last_reason:
            return
        if reason:
            log.warning("safety override: %s", reason)
        elif mode != "stop":
            # Only announce recovery when the vehicle is actually free to
            # move again -- not while we are still sitting in the stop we
            # forced a moment ago.
            log.info("safety override cleared")
        self._last_reason = reason
