"""Single-process multi-service server for the v2 sandbox (port 9100).

Mounts the service instances declared in task_config.services[] under
``/{service_id}/...`` prefixes, plus the always-on ``user_messaging`` service
under ``/user/...``. One merged OpenAPI schema (``/openapi.json``) is the
source for tool-IR generation.

Usage:
    python -m services.multi_server --task-config /config/task_config.json
    python -m services.multi_server --services email_client,slack_workspace  # smoke test

Env:
    PORT                      listen port (default 9100)
    {SERVICE_ID}_FIXTURES     fixture path per service (set from bundle, or
                              derived from --task-config fixture fields)
    ERROR_RATE                optional random 429/500 injection on POSTs (default 0)

Infra endpoints (excluded from the agent tool surface):
    GET  /_env/health         all-services health
    GET  /_env/audit          {service_id: [actions...]}  (grader convenience)
    GET  /_env/state          {service_id: state}
    POST /_env/agent_step     {"step": N} heartbeat from the harness adapter;
                              drives agent_step_count environment events
    GET  /_env/events         environment events fired so far
    GET  /_env/clock          narrative clock (now, anchor, jumps)
    POST /_env/clock          {"advance_by": "20h"} | {"set_to": ISO} — infra
                              only (replay 'wait' steps, tests)
    POST /_env/reset          reset services, events, audit, and clock
"""
from __future__ import annotations

import argparse
import copy
import hmac
import importlib
import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from services._base import INFRA_SUFFIXES, ServiceBase, fixture_env_var

USER_MESSAGING_PREFIX = "/user"


class GraderAuthMiddleware(BaseHTTPMiddleware):
    """Keep control-plane and per-service infra routes grader-only.

    ``protect_business_routes`` also requires the token on business calls.
    The OpenHands agent container can reach a loopback port through
    ``host.docker.internal`` on Docker Desktop, so the evaluator turns this
    on and the MCP gateway is the only caller that holds the token.
    """

    def __init__(self, app, grader_token: str, protect_business_routes: bool = False):
        super().__init__(app)
        self.grader_token = grader_token
        self.protect_business_routes = protect_business_routes

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        private = path != "/_env/health" and (
            path.startswith("/_env/")
            or any(path.endswith(s) for s in INFRA_SUFFIXES)
        )
        protected = private or (self.protect_business_routes and path != "/_env/health")
        if protected and self.grader_token:
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {self.grader_token}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content={"error": "grader_authorization_required"},
                )
        return await call_next(request)


# ── optional error injection (off by default; ClawEnvKit-style) ─────────────

class ErrorInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rate = float(os.environ.get("ERROR_RATE", "0"))
        path = request.url.path
        if (rate <= 0 or request.method != "POST"
                or path.startswith("/_env")
                or any(path.endswith(s) for s in INFRA_SUFFIXES)):
            return await call_next(request)
        if random.random() < rate:
            if random.random() < 0.5:
                return JSONResponse(status_code=429, content={
                    "error": "rate_limit_exceeded",
                    "message": "Too many requests. Please retry after a short delay.",
                }, headers={"Retry-After": "2"})
            return JSONResponse(status_code=500, content={
                "error": "internal_server_error",
                "message": "An unexpected error occurred. Please try again.",
            })
        return await call_next(request)


# ── environment events ───────────────────────────────────────────────────────

EVENT_TRIGGER_TYPES = ("agent_step_count", "service_call_count", "wall_clock_seconds")
EVENT_ACTION_TYPES = ("call_endpoint", "advance_clock", "set_clock")


def event_actions(event: dict) -> list[dict]:
    """Normalise an event's action(s) to a list of typed action dicts.

    Accepted shapes (task-bundle-v2.md § environment_events):
      "action":  {"endpoint": "/zoom/end_ringing", "body": {}}          (legacy)
      "action":  {"type": "advance_clock", "by": "20h"}
      "action":  {"type": "set_clock", "to": "2026-04-18T16:05:00"}
      "actions": [ ...any of the above... ]
    """
    raw = event.get("actions")
    if raw is None:
        raw = [event.get("action") or {}]
    out = []
    for a in raw:
        a = dict(a or {})
        if not a.get("type"):
            a["type"] = "call_endpoint" if a.get("endpoint") else ""
        out.append(a)
    return out


def validate_environment_events(events: list, mounted_services: set[str]) -> list[str]:
    """Static checks shared by Stage 4 (compile) and boot (fail fast)."""
    from services._clock import parse_duration, parse_iso
    problems: list[str] = []
    if not isinstance(events, list):
        return ["environment_events must be a list"]
    for i, ev in enumerate(events):
        where = f"environment_events[{i}]"
        if not isinstance(ev, dict):
            problems.append(f"{where}: not an object")
            continue
        trig = ev.get("trigger") or {}
        ttype = trig.get("type")
        if ttype not in EVENT_TRIGGER_TYPES:
            problems.append(f"{where}.trigger.type {ttype!r} not in {EVENT_TRIGGER_TYPES}")
        try:
            if float(trig.get("value", 0)) <= 0:
                problems.append(f"{where}.trigger.value must be > 0")
        except (TypeError, ValueError):
            problems.append(f"{where}.trigger.value not numeric")
        if ttype == "service_call_count":
            svc = trig.get("service", "")
            if svc not in mounted_services:
                problems.append(f"{where}.trigger.service {svc!r} not mounted "
                                f"(mounted: {sorted(mounted_services)})")
        acts = event_actions(ev)
        if not acts or all(not a.get("type") for a in acts):
            problems.append(f"{where}: no action")
        for j, a in enumerate(acts):
            aw = f"{where}.actions[{j}]"
            atype = a.get("type")
            if atype not in EVENT_ACTION_TYPES:
                problems.append(f"{aw}.type {atype!r} not in {EVENT_ACTION_TYPES}")
            elif atype == "call_endpoint":
                ep = str(a.get("endpoint", ""))
                if not ep.startswith("/"):
                    problems.append(f"{aw}.endpoint must be an absolute path")
                else:
                    sid = ep.split("/")[1]
                    if sid not in mounted_services and sid != "user":
                        problems.append(f"{aw}.endpoint targets unmounted service {sid!r}")
            elif atype == "advance_clock":
                try:
                    parse_duration(a.get("by", ""))
                except ValueError as exc:
                    problems.append(f"{aw}.by: {exc}")
            elif atype == "set_clock":
                try:
                    parse_iso(a.get("to", ""))
                except ValueError as exc:
                    problems.append(f"{aw}.to: {exc}")
    return problems


class EnvironmentEventEngine:
    """Executes task_config.environment_events (task-bundle-v2.md).

    Trigger types (all RUN-relative — never absolute dates, so a bundle
    behaves identically whenever it is run):
      agent_step_count    — driven by POST /_env/agent_step heartbeats
      service_call_count  — N business actions logged on trigger.service
                            (optionally filtered by trigger.action)
      wall_clock_seconds  — real seconds since server start (monotonic;
                            unaffected by narrative-clock jumps)
    Action types:
      call_endpoint       — POST a service route in-process (HTTP loopback),
                            so the change shows in the audit log
      advance_clock       — move narrative time forward by a duration
      set_clock           — jump narrative time to an absolute instant
    """

    def __init__(self, events: list[dict], services: dict[str, ServiceBase],
                 port: int, clock=None):
        from services._clock import get_clock
        self._events = copy.deepcopy(events)
        self.pending = [
            dict(copy.deepcopy(event), _fired=False)
            for event in self._events
        ]
        self.services = services
        self.port = port
        self.clock = clock or get_clock()
        self.app = None                 # set by create_app
        self._served_over_http = False  # set True by main() (uvicorn)
        self.agent_step = 0
        self.fired: list[dict] = []
        self._lock = threading.Lock()
        self._start_mono = time.monotonic()
        self._generation = 0

    def report_step(self, step: int) -> None:
        with self._lock:
            self.agent_step = max(self.agent_step, step)
        self.check()

    def _trigger_met(self, trigger: dict) -> bool:
        ttype = trigger.get("type")
        value = trigger.get("value", 0)
        if ttype == "wall_clock_seconds":
            return (time.monotonic() - self._start_mono) >= float(value)
        if ttype == "agent_step_count":
            return self.agent_step >= int(value)
        if ttype == "service_call_count":
            svc = self.services.get(trigger.get("service", ""))
            if svc is None:
                return False
            actions = [
                action for action in svc.get_audit()
                if action.get("source") == "agent"
            ]
            wanted = trigger.get("action")
            if wanted:
                actions = [a for a in actions if a.get("action") == wanted]
            return len(actions) >= int(value)
        return False

    def _run_action(self, action: dict, event_id: str) -> Any:
        atype = action.get("type")
        if atype == "advance_clock":
            dt = self.clock.advance(action.get("by"), reason=event_id)
            return {"clock_now": dt.isoformat(timespec="seconds")}
        if atype == "set_clock":
            dt = self.clock.set_to(action.get("to"), reason=event_id)
            return {"clock_now": dt.isoformat(timespec="seconds")}
        # call_endpoint (legacy shape: bare endpoint/body)
        endpoint = action.get("endpoint", "")
        body = action.get("body", {})
        if self.app is not None and not self._served_over_http:
            # In-process boot (Stage 5 replay / tests): no socket to loop
            # back through, drive the ASGI app directly.
            from fastapi.testclient import TestClient
            resp = TestClient(self.app).post(endpoint, json=body)
            return {"http_status": resp.status_code}
        import requests
        resp = requests.post(f"http://127.0.0.1:{self.port}{endpoint}",
                             json=body, timeout=10)
        return {"http_status": resp.status_code}

    def _execute(self, event: dict) -> None:
        event_id = str(event.get("id") or event.get("narrative") or "event")
        results = []
        for action in event_actions(event):
            try:
                results.append(self._run_action(action, event_id))
            except Exception as exc:  # noqa: BLE001 — record, never crash the env
                results.append({"error": f"{type(exc).__name__}: {exc}"})
        self.fired.append({
            "event": {k: v for k, v in event.items() if not k.startswith("_")},
            "results": results,
            "agent_step": self.agent_step,
            "narrative_time": self.clock.now_iso(),
            "timestamp": time.time(),
        })

    def check(self) -> None:
        with self._lock:
            due = [e for e in self.pending
                   if not e["_fired"] and self._trigger_met(e.get("trigger", {}))]
            for e in due:
                e["_fired"] = True
        for e in due:
            self._execute(e)

    def start_clock_watcher(self) -> None:
        if not any(e.get("trigger", {}).get("type") in ("wall_clock_seconds", "service_call_count")
                   for e in self.pending):
            return

        generation = self._generation

        def loop() -> None:
            while (generation == self._generation
                   and any(not e["_fired"] for e in self.pending)):
                self.check()
                time.sleep(2)

        threading.Thread(target=loop, daemon=True).start()

    def reset(self) -> None:
        """Restore pending events, counters, fired history, and clock."""
        with self._lock:
            self._generation += 1
            self.pending = [
                dict(copy.deepcopy(event), _fired=False)
                for event in self._events
            ]
            self.agent_step = 0
            self.fired = []
            self._start_mono = time.monotonic()
        self.clock.reset()
        self.start_clock_watcher()


# ── app assembly ─────────────────────────────────────────────────────────────

def _instantiate(service_id: str, template: str) -> Optional[ServiceBase]:
    try:
        module = importlib.import_module(f"services.{template}.service")
    except ImportError as exc:
        print(f"[multi] ERROR: cannot import template '{template}': {exc}", flush=True)
        return None
    factory = getattr(module, "create_service", None)
    if factory is None:
        print(f"[multi] ERROR: template '{template}' lacks create_service()", flush=True)
        return None
    svc = factory(service_id)
    svc.load_data()
    return svc


def create_app(service_entries: list[dict],
               environment_events: Optional[list[dict]] = None,
               port: int = 9100,
               reference_time: Optional[str] = None,
               clock_file: Optional[str] = None,
               grader_token: Optional[str] = None,
               protect_business_routes: bool = False) -> FastAPI:
    """service_entries: task_config.services[] items ({id, template, fixture?}).

    ``reference_time`` (task_config.reference_time) anchors the narrative
    clock; ``clock_file`` is the libfaketime control file when the sandbox
    runs under a frozen clock (see services/_clock.py). Both fall back to the
    SANDBOX_REFERENCE_TIME / FAKETIME_TIMESTAMP_FILE env vars set by init.sh.
    """
    from services import _clock
    clock = _clock.configure(reference_time, clock_file)

    app = FastAPI(title="Authorization Sandbox Environment API")
    app.add_middleware(ErrorInjectionMiddleware)
    app.add_middleware(
        GraderAuthMiddleware,
        grader_token=grader_token or "",
        protect_business_routes=protect_business_routes,
    )

    services: dict[str, ServiceBase] = {}

    entries = list(service_entries)
    if not any(e.get("template") == "user_messaging" for e in entries):
        entries.append({"id": "user_messaging", "template": "user_messaging"})

    bad_events = validate_environment_events(
        list(environment_events or []), {e["id"] for e in entries})
    if bad_events:
        # Fail loudly: a mis-compiled event silently never firing is exactly
        # the "sale never happens" failure mode we are trying to eliminate.
        raise ValueError("invalid environment_events: " + "; ".join(bad_events))

    for entry in entries:
        service_id = entry["id"]
        template = entry.get("template", service_id)
        fixture = entry.get("fixture")
        if fixture:
            os.environ[fixture_env_var(service_id)] = str(fixture)
        svc = _instantiate(service_id, template)
        if svc is None:
            continue
        prefix = USER_MESSAGING_PREFIX if template == "user_messaging" else f"/{service_id}"
        app.include_router(svc.router, prefix=prefix, tags=[service_id])
        services[service_id] = svc

    def resolve_mounted_service(reference: str) -> Optional[ServiceBase]:
        if reference in services:
            return services[reference]
        matches = [
            service for service in services.values()
            if service.template == reference
        ]
        return matches[0] if len(matches) == 1 else None

    for service in services.values():
        service.bind_service_resolver(resolve_mounted_service)

    print(f"[multi] Mounted {len(services)} services: {', '.join(services)}", flush=True)

    engine = EnvironmentEventEngine(environment_events or [], services, port, clock=clock)
    engine.app = app
    app.state.services = services
    app.state.event_engine = engine
    app.state.clock = clock

    if engine.pending:
        from starlette.concurrency import run_in_threadpool

        @app.middleware("http")
        async def _check_events_after_business_call(request: Request, call_next):
            # Deterministic trigger evaluation: service_call_count (and any
            # clock-based state) is re-checked right after every business
            # action, so in-process replays see events fire at the exact
            # step, not whenever the 2s watcher thread happens to wake.
            response = await call_next(request)
            path = request.url.path
            if (request.method == "POST" and not path.startswith("/_env")
                    and not any(path.endswith(s) for s in INFRA_SUFFIXES)):
                await run_in_threadpool(engine.check)
            return response

    @app.get("/_env/health", include_in_schema=False)
    def _health() -> dict:
        return {"status": "ok", "services": sorted(services),
                "narrative_time": clock.now_iso()}

    @app.get("/_env/clock", include_in_schema=False)
    def _clock_get() -> dict:
        return clock.describe()

    @app.post("/_env/clock", include_in_schema=False)
    def _clock_set(payload: dict) -> dict:
        """Infra-only: {"advance_by": "20h"} or {"set_to": "2026-04-18T16:05:00"}.
        Used by Stage 5 replay 'wait' steps and by tests; never a tool."""
        reason = str(payload.get("reason") or "manual")
        if payload.get("advance_by") is not None:
            clock.advance(payload["advance_by"], reason=reason)
        elif payload.get("set_to"):
            clock.set_to(payload["set_to"], reason=reason)
        else:
            return JSONResponse(status_code=400, content={
                "error": "expected advance_by or set_to"})
        engine.check()
        return clock.describe()

    @app.get("/_env/audit", include_in_schema=False)
    def _audit_all() -> dict:
        return {sid: svc.get_audit() for sid, svc in services.items()}

    @app.get("/_env/state", include_in_schema=False)
    def _state_all() -> dict:
        return {sid: svc.get_state() for sid, svc in services.items()}

    @app.post("/_env/agent_step", include_in_schema=False)
    def _agent_step(payload: dict) -> dict:
        engine.report_step(int(payload.get("step", 0)))
        return {"status": "ok", "agent_step": engine.agent_step}

    @app.post("/_env/reset", include_in_schema=False)
    def _reset_environment() -> dict:
        for service in services.values():
            service.reset()
        engine.reset()
        return {
            "status": "reset",
            "services": sorted(services),
            "narrative_time": clock.now_iso(),
        }

    @app.get("/_env/events", include_in_schema=False)
    def _events() -> dict:
        return {"fired": engine.fired,
                "pending": [{k: v for k, v in e.items() if not k.startswith("_")}
                            for e in engine.pending if not e["_fired"]]}

    @app.on_event("startup")
    def _start_engine() -> None:
        engine.start_clock_watcher()

    return app


def _disable_llm_tls_time_validation() -> None:
    """In-container LLM calls (Oracle, calendar sync-NPC) run under the frozen
    faketime clock, so TLS certificate validity windows (notBefore/notAfter)
    are checked against task_config.reference_time instead of the real date.
    Any reference_time more than a few weeks away from today makes every
    handshake fail with 'certificate is not yet valid/expired'. We disable
    cert verification for the environment's own outbound litellm calls only;
    the agent's tools are unaffected. This decouples task reference_time from
    the run date entirely."""
    try:
        import litellm
        litellm.ssl_verify = False
    except ImportError:
        pass
    # litellm reads this env for some client paths; harmless if unused.
    os.environ.setdefault("SSL_VERIFY", "False")


ORACLE_PROFILE = "mimo_v2_6_pro_oracle"


def configure_oracle_runtime(task_config: str | None) -> None:
    """Point message_user at this task and the Oracle model profile.

    The container path sets LITELLM_* itself. A host run that only passes
    ``--task-config`` still needs the task file and the ``ORACLE_PROFILE``
    credentials, or message_user returns 500.
    """
    if task_config:
        os.environ["TASK_CONFIG"] = str(Path(task_config).resolve())
    if os.environ.get("LITELLM_API_KEY"):
        return
    config_path = Path(__file__).resolve().parent.parent / "evaluation" / "config.toml"
    if not config_path.is_file():
        return
    import tomllib
    profile = (
        tomllib.loads(config_path.read_text()).get("llm") or {}
    ).get(ORACLE_PROFILE) or {}
    if not profile.get("api_key"):
        return
    os.environ["LITELLM_MODEL"] = str(profile.get("model") or "")
    os.environ["LITELLM_API_KEY"] = str(profile["api_key"])
    os.environ["LITELLM_BASE_URL"] = str(profile.get("base_url") or "")
    if profile.get("completion_kwargs"):
        os.environ["LITELLM_COMPLETION_KWARGS"] = json.dumps(
            profile["completion_kwargs"]
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-config", type=str, default=os.environ.get(
        "TASK_CONFIG", "/config/task_config.json"))
    parser.add_argument("--services", type=str, default="",
                        help="Comma-separated template names (smoke-test mode, no fixtures)")
    args = parser.parse_args()

    environment_events: list[dict] = []
    reference_time: Optional[str] = None
    grader_token = os.environ.get("GRADER_TOKEN", "")
    if args.services:
        entries = [{"id": s.strip(), "template": s.strip()}
                   for s in args.services.split(",") if s.strip()]
    else:
        if not grader_token:
            raise SystemExit("GRADER_TOKEN is required for task runtime")
        config = json.loads(Path(args.task_config).read_text())
        entries = config.get("services", [])
        environment_events = config.get("environment_events", [])
        reference_time = config.get("reference_time") or None
        # Resolve bundle-relative fixture paths against the config's directory.
        base = Path(args.task_config).parent
        for e in entries:
            if e.get("fixture") and not Path(e["fixture"]).is_absolute():
                e["fixture"] = str(base / e["fixture"])

    if not args.services:
        configure_oracle_runtime(args.task_config)

    _disable_llm_tls_time_validation()

    port = int(os.environ.get("PORT", "9100"))
    app = create_app(entries, environment_events, port=port,
                     reference_time=reference_time,
                     grader_token=grader_token)
    app.state.event_engine._served_over_http = True
    print(f"[multi] narrative clock: {app.state.clock.describe()}", flush=True)

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
