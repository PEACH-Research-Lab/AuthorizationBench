"""Base class + helpers for v2 API mock services.

Every service template module under services/<template>/service.py exposes:

    def create_service(service_id: str) -> ServiceBase

The factory form (instead of a module-level singleton) lets one task mount
two instances of the same template under different ids (e.g. ``email_work``
and ``email_personal``) with independent state.

A ServiceBase owns an APIRouter. multi_server includes each service's router
with ``prefix=f"/{service_id}"`` so the combined app has one merged OpenAPI
schema — that schema is what the tool-IR generator (eval-tools.json) reads.

Infra endpoints provided automatically on every service:

    GET  /{svc}/audit    -> {"actions": [{action, params, timestamp, status}, ...]}
    GET  /{svc}/state    -> full business state (service-state diff detector)
    POST /{svc}/reset    -> reload fixtures, clear audit
    GET  /{svc}/health   -> {"status": "ok"}

These infra routes are excluded from the agent tool surface (the tool-IR
generator skips them by path suffix) and from error injection.
"""
from __future__ import annotations

import json
import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

INFRA_SUFFIXES = ("/audit", "/state", "/reset", "/health", "/inject")


def fixture_env_var(service_id: str) -> str:
    return f"{service_id.upper()}_FIXTURES"


def load_fixture(service_id: str) -> dict:
    """Load this service instance's fixture JSON.

    Path comes from the ``{SERVICE_ID}_FIXTURES`` env var (set by init.sh /
    multi_server from the task bundle). Always returns a dict; missing or
    invalid files yield {} so a service can boot empty for smoke tests.
    """
    path = os.environ.get(fixture_env_var(service_id), "")
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {"items": data}


# ── fixture schema (contract-implementation-test triangle) ──────────────────
#
# A template may declare ``FIXTURE_SCHEMA`` as a class attribute right next to
# ``load_data`` — the DECLARED contract of its fixture file. ``load_data`` is
# the implementation; ``tests/test_fixture_schema_conformance.py`` binds the
# two so they cannot drift silently. The registry derives the schema live
# (like the action inventory) — it is never copied into a second file.
#
# Encoding (JSON-serializable, prompt-friendly):
#   "str" | "int" | "float" | "bool" | "list" | "dict"   scalar leaf type
#   "enum:a|b|c"                                          string restricted to
#                                                         the listed values (a
#                                                         state machine field —
#                                                         NOT free text)
#   {...}                                                 nested object
#   [{...}]                                               list of records
#
# Validation is strict on TOP-LEVEL keys (catches typos like "email" vs
# "emails") and lenient inside objects/records (services tolerate extras;
# only declared fields are type-checked). Every key is optional — services
# boot empty (load_fixture returns {} for smoke tests).

FIXTURE_TYPES = ("str", "int", "float", "bool", "list", "dict")
ENUM_PREFIX = "enum:"

_PY_OF_TYPE = {"str": str, "int": int, "float": (int, float),
               "bool": bool, "list": list, "dict": dict}


def enum_values(ftype: str) -> Optional[list[str]]:
    """``"enum:a|b|c"`` -> ``["a", "b", "c"]``; None for non-enum specs."""
    if isinstance(ftype, str) and ftype.startswith(ENUM_PREFIX):
        return [v for v in ftype[len(ENUM_PREFIX):].split("|") if v]
    return None


def is_valid_leaf_type(ftype: Any) -> bool:
    if ftype in FIXTURE_TYPES:
        return True
    values = enum_values(ftype)
    return bool(values)


def _check_leaf(value: Any, ftype: str, where: str, issues: list[str]) -> None:
    allowed = enum_values(ftype)
    if allowed is not None:
        if not isinstance(value, str):
            issues.append(f"{where}: expected one of {allowed}, "
                          f"got {type(value).__name__}")
        elif value not in allowed:
            shown = value if len(value) <= 60 else value[:57] + "..."
            issues.append(f"{where}: expected one of {allowed}, got {shown!r} "
                          "— this is a state field, not free text; put prose "
                          "in a str field")
        return
    expected = _PY_OF_TYPE[ftype]
    if isinstance(value, bool) and ftype in ("int", "float"):
        issues.append(f"{where}: expected {ftype}, got bool")
    elif not isinstance(value, expected):
        issues.append(f"{where}: expected {ftype}, got {type(value).__name__}")


def _validate_node(value: Any, spec: Any, where: str, issues: list[str]) -> None:
    if isinstance(spec, str):
        _check_leaf(value, spec, where, issues)
    elif isinstance(spec, dict):
        if not isinstance(value, dict):
            issues.append(f"{where}: expected object, got {type(value).__name__}")
            return
        for k, sub in spec.items():
            if k in value:
                _validate_node(value[k], sub, f"{where}.{k}", issues)
    elif isinstance(spec, list) and spec:
        if not isinstance(value, list):
            issues.append(f"{where}: expected list, got {type(value).__name__}")
            return
        for i, item in enumerate(value):
            _validate_node(item, spec[0], f"{where}[{i}]", issues)


def validate_fixture(data: dict, schema: dict) -> list[str]:
    """Check a fixture dict against a FIXTURE_SCHEMA. Returns problems."""
    issues: list[str] = []
    if not isinstance(data, dict):
        return [f"fixture root: expected object, got {type(data).__name__}"]
    for key in data:
        if key not in schema:
            issues.append(f"unknown top-level key {key!r} "
                          f"(schema keys: {sorted(schema)})")
    for key, spec in schema.items():
        if key in data:
            _validate_node(data[key], spec, key, issues)
    return issues


def synthetic_fixture(schema: dict) -> dict:
    """Build a minimal fixture (one record per collection) from a schema."""
    def _node(spec: Any, name: str) -> Any:
        if isinstance(spec, str):
            allowed = enum_values(spec)
            if allowed:
                return allowed[0]
            if spec == "str" and name == "timezone":
                return "UTC"
            if spec == "str" and name == "calendar_id":
                return "seed-id"
            return {"str": f"seed-{name}", "int": 1, "float": 1.5,
                    "bool": True, "list": [], "dict": {}}[spec]
        if isinstance(spec, dict):
            return {k: _node(v, k) for k, v in spec.items()}
        if isinstance(spec, list) and spec:
            return [_node(spec[0], name)]
        return None
    return {k: _node(v, k) for k, v in schema.items()}


def find_scripted_reply(scripted_replies: list[dict], text: str) -> Optional[str]:
    """Match an outgoing message against fixture-defined scripted replies.

    Fixture shape: ``[{"match": "substring or *", "reply": "..."}]``.
    First match wins; ``"*"`` matches anything. Used by messaging-type
    services to keep the v1 scripted-reply-trap mechanic without live NPCs.
    """
    lowered = text.lower()
    for entry in scripted_replies:
        pattern = str(entry.get("match", "*"))
        if pattern == "*" or pattern.lower() in lowered:
            return entry.get("reply")
    return None


class ServiceBase:
    """One mounted service instance: router + audit log + fixture state."""

    def __init__(self, service_id: str, template: str):
        self.service_id = service_id
        self.template = template
        self.router = APIRouter()
        self._audit_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._audit_sequence = 0
        self._id_counters: dict[str, int] = {}
        self._service_resolver: Optional[
            Callable[[str], Optional["ServiceBase"]]
        ] = None
        self.audit_log: list[dict] = []
        # Optional hook for future interactive NPCs; v2 ships scripted-only.
        self.reply_hook: Optional[Callable[[str, str], Optional[str]]] = None
        self._register_infra_routes()

    # ── to be implemented by templates ──────────────────────────────────────

    def load_data(self) -> None:
        """(Re)load business state from the fixture. Called at boot and reset."""
        raise NotImplementedError

    def get_state(self) -> dict:
        """Full business state for the service-state diff detector."""
        raise NotImplementedError

    def inject(self, content: dict) -> dict:
        """Inject state from OUTSIDE the agent's action space (environment
        events, scripted world changes). Bypasses any agent-facing policy
        (whitelists, rate limits). Template-specific; default: unsupported.
        """
        raise NotImplementedError(f"inject not implemented for {self.template}")

    def bind_service_resolver(
        self,
        resolver: Callable[[str], Optional["ServiceBase"]],
    ) -> None:
        """Bind the runtime-owned lookup used by declared cross-service calls."""
        self._service_resolver = resolver

    def resolve_service(self, service_or_template: str) -> Optional["ServiceBase"]:
        """Resolve a mounted service without exposing the registry to templates."""
        if self._service_resolver is None:
            return None
        return self._service_resolver(str(service_or_template))

    # ── audit ────────────────────────────────────────────────────────────────

    def now_iso(self) -> str:
        """Current deterministic narrative timestamp."""
        from services._clock import now_iso

        return now_iso()

    def new_id(self, kind: str) -> str:
        """Return a stable per-run ID independent of wall clock and randomness."""
        key = str(kind or "record")
        with self._state_lock:
            ordinal = self._id_counters.get(key, 0) + 1
            self._id_counters[key] = ordinal
        digest = hashlib.sha256(
            f"{self.service_id}\0{key}\0{ordinal}".encode()
        ).hexdigest()[:10]
        normalized = "".join(
            char if char.isalnum() else "-" for char in key.lower()
        ).strip("-") or "record"
        return f"{normalized}-{digest}"

    @staticmethod
    def error_response(
        *,
        code: str,
        message: str,
        status: int,
        details: Optional[dict[str, Any]] = None,
    ) -> JSONResponse:
        payload: dict[str, Any] = {
            "error": {
                "code": str(code),
                "message": str(message),
            }
        }
        if details:
            payload["error"]["details"] = details
        return JSONResponse(status_code=status, content=payload)

    def log_action(self, action: str, params: Optional[dict] = None,
                   status: int = 200, source: str = "agent",
                   effect: Optional[dict[str, Any]] = None,
                   **details: Any) -> None:
        """Record an agent-visible action (TrajectoryLog web_app_action_logs shape)."""
        with self._audit_lock:
            self._audit_sequence += 1
            entry = {
                "sequence": self._audit_sequence,
                "source": source,
                "action": action,
                "params": params or {},
                "timestamp": self.now_iso(),
                "status": status,
            }
            if effect is not None:
                entry["effect"] = effect
            if details:
                entry.update(details)
            self.audit_log.append(entry)

    def get_audit(self) -> list[dict]:
        with self._audit_lock:
            return list(self.audit_log)

    # ── infra routes ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Restore fixture-backed state and deterministic per-run counters."""
        with self._audit_lock:
            self.audit_log = []
            self._audit_sequence = 0
        with self._state_lock:
            self._id_counters = {}
        self.load_data()

    def _register_infra_routes(self) -> None:
        @self.router.get("/audit", include_in_schema=False)
        def _audit() -> dict:
            return {"actions": self.get_audit()}

        @self.router.get("/state", include_in_schema=False)
        def _state() -> dict:
            return {"service_id": self.service_id, "template": self.template,
                    "state": self.get_state()}

        @self.router.post("/reset", include_in_schema=False)
        def _reset() -> dict:
            self.reset()
            return {"status": "reset"}

        @self.router.get("/health", include_in_schema=False)
        def _health() -> dict:
            return {"status": "ok", "service_id": self.service_id}

        @self.router.post("/inject", include_in_schema=False)
        def _inject(content: dict) -> dict:
            return self.inject(content)
