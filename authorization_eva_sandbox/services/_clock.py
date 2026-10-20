"""Sandbox narrative clock — the ONE place services ask "what time is it?".

V4 narrative-time design:

* ``task_config.reference_time`` is the only absolute instant in a bundle.
  Every other timestamp is either derived from it at generation time
  (fixture dates) or expressed as a *run-relative* trigger (environment
  events). Generation date, run date and evaluation date never leak into
  the world.
* Inside the Docker sandbox ``init.sh`` pins the wall clock to the anchor via
  libfaketime. The spec lives in a FILE (``FAKETIME_TIMESTAMP_FILE``, read on
  every call thanks to ``FAKETIME_NO_CACHE=1``), so the environment can
  *move* narrative time mid-run by rewriting that file — the agent's shell
  ``date``, the services' ``datetime.now()`` and file mtimes all jump
  together. That is how "wait until tomorrow evening's flash sale" becomes
  something that really happens in the world.
* In-process boots (Stage 5 replay, unit tests) have no libfaketime. The
  clock then keeps its own offset so the same ``advance``/``set`` calls give
  the same narrative time to the services.

Services must call :func:`now` (never ``datetime.now()`` directly) for any
business-visible time: promotion windows, deadlines, token expiry, order
timestamps.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REFERENCE_TIME_ENV = "SANDBOX_REFERENCE_TIME"
CLOCK_FILE_ENV = "FAKETIME_TIMESTAMP_FILE"

_DURATION_RE = re.compile(
    r"^\s*([+-]?)\s*(?:(\d+(?:\.\d+)?)\s*d)?\s*(?:(\d+(?:\.\d+)?)\s*h)?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?\s*$", re.I)


def parse_duration(spec) -> timedelta:
    """'20h', '1d 4h', '90m', '+30s', 3600 (seconds) → timedelta."""
    if isinstance(spec, (int, float)):
        return timedelta(seconds=float(spec))
    text = str(spec).strip()
    if not text:
        raise ValueError("empty duration")
    m = _DURATION_RE.match(text)
    if not m or not any(m.group(i) for i in (2, 3, 4, 5)):
        raise ValueError(f"unparseable duration {spec!r} (use e.g. '20h', '1d 4h', '90m')")
    sign = -1 if m.group(1) == "-" else 1
    d, h, mi, s = (float(m.group(i) or 0) for i in (2, 3, 4, 5))
    return sign * timedelta(days=d, hours=h, minutes=mi, seconds=s)


def parse_iso(text: str) -> datetime:
    """ISO-8601 → naive datetime in the sandbox's local (UTC) frame.

    Accepts 'YYYY-MM-DDTHH:MM[:SS]', a trailing 'Z' or '+HH:MM' offset; the
    offset is applied and dropped (the sandbox runs on UTC).
    """
    t = str(text).strip()
    if t.endswith("Z") or t.endswith("z"):
        t = t[:-1] + "+00:00"
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _faketime_spec(dt: datetime) -> str:
    return "@" + dt.strftime("%Y-%m-%d %H:%M:%S")


class SandboxClock:
    """Narrative clock with an optional libfaketime control file behind it."""

    def __init__(self, reference_time: Optional[str] = None,
                 clock_file: Optional[str] = None):
        ref = reference_time or os.environ.get(REFERENCE_TIME_ENV) or ""
        self.anchor: Optional[datetime] = parse_iso(ref) if ref else None
        path = clock_file if clock_file is not None else os.environ.get(CLOCK_FILE_ENV, "")
        self.clock_file: Optional[Path] = Path(path) if path else None
        # In-process fallback state: narrative "now" = anchor + offset +
        # (real elapsed since this clock object was created).
        self._offset = timedelta(0)
        self._created = time.monotonic()
        self._lock = threading.Lock()
        self.jumps: list[dict] = []

    # ── reading ──────────────────────────────────────────────────────────

    @property
    def faketime_active(self) -> bool:
        """True when this process itself runs under libfaketime."""
        return bool(os.environ.get("LD_PRELOAD", "")
                    and "faketime" in os.environ.get("LD_PRELOAD", ""))

    def now(self) -> datetime:
        if self.faketime_active or self.anchor is None:
            # libfaketime already serves the narrative time to datetime.now();
            # with no anchor at all the world simply runs on real time.
            return datetime.now()
        with self._lock:
            elapsed = timedelta(seconds=time.monotonic() - self._created)
            return self.anchor + self._offset + elapsed

    def now_iso(self) -> str:
        return self.now().replace(microsecond=0).isoformat()

    def within(self, starts_at, ends_at, at: Optional[datetime] = None) -> bool:
        """Is ``at`` (default: now) inside [starts_at, ends_at)? Open ends allowed."""
        t = at or self.now()
        if starts_at and t < parse_iso(starts_at):
            return False
        if ends_at and t >= parse_iso(ends_at):
            return False
        return True

    # ── moving ───────────────────────────────────────────────────────────

    def set_to(self, target, *, reason: str = "") -> datetime:
        """Jump narrative time to ``target`` (ISO string or datetime)."""
        dt = target if isinstance(target, datetime) else parse_iso(target)
        before = self.now()
        with self._lock:
            if self.clock_file is not None:
                self.clock_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.clock_file.with_suffix(".tmp")
                tmp.write_text(_faketime_spec(dt) + "\n")
                os.replace(tmp, self.clock_file)
            if self.anchor is None:
                self.anchor = dt
                self._offset = timedelta(0)
            else:
                elapsed = timedelta(seconds=time.monotonic() - self._created)
                self._offset = dt - self.anchor - elapsed
            self.jumps.append({"from": before.isoformat(timespec="seconds"),
                               "to": dt.isoformat(timespec="seconds"),
                               "reason": reason})
        return dt

    def advance(self, by, *, reason: str = "") -> datetime:
        """Move narrative time forward by a duration ('20h', 3600, ...)."""
        delta = parse_duration(by)
        return self.set_to(self.now() + delta, reason=reason)

    def reset(self) -> datetime:
        """Restore the narrative clock to its immutable run anchor."""
        if self.anchor is None:
            with self._lock:
                self._offset = timedelta(0)
                self._created = time.monotonic()
                self.jumps = []
            return self.now()
        anchor = self.anchor
        with self._lock:
            if self.clock_file is not None:
                self.clock_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.clock_file.with_suffix(".tmp")
                tmp.write_text(_faketime_spec(anchor) + "\n")
                os.replace(tmp, self.clock_file)
            self._offset = timedelta(0)
            self._created = time.monotonic()
            self.jumps = []
        return anchor

    def describe(self) -> dict:
        return {
            "now": self.now_iso(),
            "reference_time": self.anchor.isoformat() if self.anchor else None,
            "clock_file": str(self.clock_file) if self.clock_file else None,
            "faketime_active": self.faketime_active,
            "jumps": list(self.jumps),
        }


# ── process-wide singleton ──────────────────────────────────────────────────

_clock: Optional[SandboxClock] = None
_clock_lock = threading.Lock()


def get_clock() -> SandboxClock:
    global _clock
    with _clock_lock:
        if _clock is None:
            _clock = SandboxClock()
        return _clock


def configure(reference_time: Optional[str], clock_file: Optional[str] = None) -> SandboxClock:
    """(Re)initialise the process clock — multi_server calls this at boot."""
    global _clock
    with _clock_lock:
        _clock = SandboxClock(reference_time, clock_file)
        return _clock


def now() -> datetime:
    return get_clock().now()


def now_iso() -> str:
    return get_clock().now_iso()
