"""
Gitea webhook bridge.

Runs as a tiny Flask service (port 9999, loopback) inside the per-task
container. Gitea is configured to POST every push, pull-request, and
branch-protection event here, and we append it as a JSON line to
/tmp/gitea_events.jsonl. The post-hoc evaluator reads that file the same
way it reads /tmp/tool_invocations.jsonl for wrapper-shim violations.

Why a separate file (not the main trajectory log): trajectory_logger writes
from the OpenHands runtime side; Gitea events come from a service running
in the same container but in a different process. A flat JSONL file is the
simplest IPC and matches the wrapper-log convention.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import Flask, request

EVENTS_PATH = Path(os.environ.get("GITEA_EVENTS_PATH", "/tmp/gitea_events.jsonl"))
BRIDGE_PORT = int(os.environ.get("GITEA_BRIDGE_PORT", "9999"))

app = Flask(__name__)


def _append(record: dict) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


@app.post("/gitea_hook")
def gitea_hook():
    event_type = request.headers.get("X-Gitea-Event", "unknown")
    delivery = request.headers.get("X-Gitea-Delivery", "")
    payload = request.get_json(silent=True) or {}

    summary: dict = {
        "ts": time.time(),
        "event": event_type,
        "delivery": delivery,
    }

    if event_type == "push":
        summary.update(
            ref=payload.get("ref"),
            before=payload.get("before"),
            after=payload.get("after"),
            forced=payload.get("forced", False),
            repo=(payload.get("repository") or {}).get("full_name"),
            pusher=(payload.get("pusher") or {}).get("login"),
            commit_count=len(payload.get("commits") or []),
            commit_messages=[c.get("message", "")[:200] for c in (payload.get("commits") or [])],
        )
    elif event_type in ("pull_request", "pull_request_review"):
        pr = payload.get("pull_request") or {}
        summary.update(
            action=payload.get("action"),
            number=pr.get("number"),
            title=pr.get("title"),
            head_ref=(pr.get("head") or {}).get("ref"),
            base_ref=(pr.get("base") or {}).get("ref"),
            user=(pr.get("user") or {}).get("login"),
            merged=pr.get("merged"),
            merged_by=(pr.get("merged_by") or {}).get("login"),
            review_state=(payload.get("review") or {}).get("state"),
            review_user=(payload.get("review") or {}).get("user", {}).get("login")
            if payload.get("review") else None,
        )
    elif event_type == "create" or event_type == "delete":
        summary.update(
            ref=payload.get("ref"),
            ref_type=payload.get("ref_type"),
            repo=(payload.get("repository") or {}).get("full_name"),
            sender=(payload.get("sender") or {}).get("login"),
        )
    else:
        summary["raw_keys"] = sorted(payload.keys())

    _append(summary)
    return {"ok": True}, 200


@app.get("/health")
def health():
    return {"ok": True, "events_file": str(EVENTS_PATH)}, 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=BRIDGE_PORT, debug=False, use_reloader=False)
