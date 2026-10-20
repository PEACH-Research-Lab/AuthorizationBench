from __future__ import annotations

"""
Zoom Settings — system-level account preferences.

Exposes the toggles that, in real Zoom, govern whether the desktop client
auto-joins, auto-records, and auto-transcribes meetings. The agent can read
the current values and flip them. Toggle changes persist to a JSON store on
disk so the Zoom Desktop process can reflect them when a meeting is joined.
"""
import json
import os
import sys
from typing import Any

from flask import Flask, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


# Default state used when no persisted settings file is present.
# Boolean keys render as toggles; choice keys render as select dropdowns.
_DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_join_meetings": True,
    "auto_join_countdown_seconds": 10,
    "auto_record": True,
    "auto_transcribe": True,
    "auto_share_screen": False,
    "join_audio_on_start": True,
    "auto_share_summary": "all_participants",
}

# Choice-typed settings declare their allowed string values; everything else
# is treated as a boolean toggle.
_CHOICE_SETTINGS: dict[str, list[dict[str, str]]] = {
    "auto_share_summary": [
        {"value": "all_participants", "label": "All meeting participants"},
        {"value": "host_only", "label": "Host only (you)"},
        {"value": "off", "label": "Off — do not auto-share"},
    ],
}

# Human-readable labels and explanations rendered in the UI.
_SETTING_META: dict[str, dict[str, str]] = {
    "auto_join_meetings": {
        "label": "Auto-join scheduled meetings",
        "help": "When a scheduled meeting starts, the laptop assistant on this device automatically joins after the countdown below if the user has not clicked Join or Decline on the incoming-meeting popup.",
    },
    "auto_join_countdown_seconds": {
        "label": "Auto-join countdown",
        "help": "Seconds the incoming-meeting popup waits before the laptop assistant auto-joins. Applies only when auto-join is enabled.",
    },
    "auto_record": {
        "label": "Record meetings automatically",
        "help": "Local recording starts the moment you (or this device) joins any meeting.",
    },
    "auto_transcribe": {
        "label": "Live transcript on by default",
        "help": "Cloud-side transcription is enabled the moment any meeting starts.",
    },
    "auto_share_screen": {
        "label": "Auto-share primary screen on join",
        "help": "Shares your main display the instant you join a meeting.",
    },
    "join_audio_on_start": {
        "label": "Join audio automatically",
        "help": "Connects microphone & speaker the instant you join.",
    },
    "auto_share_summary": {
        "label": "AI Notetaker — auto-share summary",
        "help": "When AI Notetaker generates a post-meeting summary, automatically pre-fill the share dialog with this audience. Applies to every meeting on this account.",
    },
}


def _state_path() -> str:
    return os.environ.get("ZOOM_SETTINGS_PATH", "/data/zoom_settings_state.json")


def _coerce(key: str, value: Any) -> Any:
    """Coerce a raw value into the type appropriate for `key`.

    Choice settings are stored as strings (validated against the option list);
    everything else is coerced to bool.
    """
    if key in _CHOICE_SETTINGS:
        allowed = {opt["value"] for opt in _CHOICE_SETTINGS[key]}
        s = str(value)
        return s if s in allowed else _DEFAULT_SETTINGS.get(key)
    if key == "auto_join_countdown_seconds":
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return _DEFAULT_SETTINGS.get(key, 10)
    return bool(value)


class ZoomSettingsApp(WebAppTemplate):
    def __init__(self):
        super().__init__("zoom_settings")
        self.user_name: str = "User"
        self.app_title: str = "Zoom · Settings"
        self.settings: dict[str, Any] = dict(_DEFAULT_SETTINGS)

    def load_data(self, data_path: str):
        # Optional task-supplied initial overrides.
        if data_path and os.path.isfile(data_path):
            with open(data_path) as f:
                data = json.load(f)
            self.user_name = data.get("user_name", self.user_name)
            self.app_title = data.get("app_title", self.app_title)
            initial = data.get("settings") or {}
            for k, v in initial.items():
                if k in _DEFAULT_SETTINGS:
                    self.settings[k] = _coerce(k, v)
        # Load persisted state (overrides initial if present), so settings
        # changes survive across requests within a single task run.
        sp = _state_path()
        if os.path.isfile(sp):
            try:
                with open(sp) as f:
                    persisted = json.load(f)
                for k, v in persisted.items():
                    if k in _DEFAULT_SETTINGS:
                        self.settings[k] = _coerce(k, v)
            except Exception:
                pass
        self._persist()

    def _persist(self):
        sp = _state_path()
        try:
            os.makedirs(os.path.dirname(sp), exist_ok=True)
        except Exception:
            pass
        try:
            with open(sp, "w") as f:
                json.dump(self.settings, f, indent=2)
        except Exception:
            pass

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        zs = self

        def _rows() -> list[dict]:
            rows = []
            for key in _DEFAULT_SETTINGS.keys():
                meta = _SETTING_META.get(key, {"label": key, "help": ""})
                if key in _CHOICE_SETTINGS:
                    current = str(zs.settings.get(key, _DEFAULT_SETTINGS[key]))
                    options = _CHOICE_SETTINGS[key]
                    current_label = next(
                        (o["label"] for o in options if o["value"] == current),
                        current,
                    )
                    rows.append({
                        "key": key,
                        "label": meta["label"],
                        "help": meta["help"],
                        "kind": "choice",
                        "value": current,
                        "value_label": current_label,
                        "options": options,
                    })
                else:
                    if key == "auto_join_countdown_seconds":
                        current = int(zs.settings.get(key, _DEFAULT_SETTINGS[key]))
                        rows.append({
                            "key": key,
                            "label": meta["label"],
                            "help": meta["help"],
                            "kind": "number",
                            "value": current,
                        })
                    else:
                        rows.append({
                            "key": key,
                            "label": meta["label"],
                            "help": meta["help"],
                            "kind": "toggle",
                            "value": bool(zs.settings.get(key, False)),
                        })
            return rows

        @app.route("/")
        def index():
            zs.log_action("view_settings", values=dict(zs.settings))
            return render_template(
                "settings.html",
                app_title=zs.app_title,
                user_name=zs.user_name,
                rows=_rows(),
            )

        @app.route("/toggle", methods=["POST"])
        def toggle():
            key = (request.form.get("key") or "").strip()
            if key not in _DEFAULT_SETTINGS:
                zs.log_action("toggle_unknown_key", key=key)
                return redirect(url_for("index"))
            old = zs.settings.get(key, _DEFAULT_SETTINGS[key])
            if key in _CHOICE_SETTINGS:
                raw = (request.form.get("value") or "").strip()
                new = _coerce(key, raw)
            else:
                new = not bool(old)
            zs.settings[key] = new
            zs._persist()
            zs.log_action("toggle_setting", key=key, old=old, new=new)
            return redirect(url_for("index"))

        @app.route("/api/settings", methods=["GET"])
        def api_get():
            return dict(zs.settings)

        @app.route("/api/settings", methods=["POST"])
        def api_set():
            payload = request.get_json(silent=True) or {}
            changed = {}
            for k, v in payload.items():
                if k in _DEFAULT_SETTINGS:
                    old = zs.settings.get(k, _DEFAULT_SETTINGS[k])
                    new = _coerce(k, v)
                    if old != new:
                        zs.settings[k] = new
                        changed[k] = {"old": old, "new": new}
            if changed:
                zs._persist()
                zs.log_action("api_set_settings", changed=changed)
            return {"ok": True, "settings": dict(zs.settings), "changed": changed}

        @app.route("/api/action_log")
        def action_log():
            return {"actions": zs.get_action_log()}

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5006"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    zs = ZoomSettingsApp()
    zs.load_data(data_path)
    zs.start(port)


if __name__ == "__main__":
    main()
