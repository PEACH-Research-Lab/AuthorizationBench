"""life_log service — recorded health, sleep, and focus entries.

Reduced from LifeOps health / sleep / focus to fixture-backed records:
    list_metrics, log_metric, list_sleep, log_sleep, list_focus, log_focus

Deterministic fixture state only. No wearables, devices, network, OAuth, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ListMetricsRequest(BaseModel):
    metric: str = Field(
        "",
        description="Optional metric type filter (e.g. weight_kg, steps).",
    )
    metric_id: str = Field(
        "",
        description="Optional single metric id; 404 when absent from state.",
    )


class LogMetricRequest(BaseModel):
    metric: str = Field(..., description="Metric type (e.g. weight_kg, steps).")
    value: float = Field(..., description="Numeric metric value.")
    unit: str = Field("", description="Optional unit label (kg, count, bpm).")
    recorded_at: str = Field(
        "",
        description="ISO datetime when the reading occurred; defaults to now.",
    )
    note: str = ""


class ListSleepRequest(BaseModel):
    sleep_id: str = Field(
        "",
        description="Optional single sleep session id; 404 when absent.",
    )


class LogSleepRequest(BaseModel):
    start_at: str = Field(..., description="ISO datetime when sleep began.")
    end_at: str = Field("", description="ISO datetime when sleep ended.")
    duration_minutes: float = Field(
        0.0,
        description="Optional duration in minutes when end_at is omitted.",
    )
    quality: str = Field(
        "fair",
        description="poor | fair | good | excellent",
    )
    note: str = ""


class ListFocusRequest(BaseModel):
    focus_id: str = Field(
        "",
        description="Optional single focus session id; 404 when absent.",
    )


class LogFocusRequest(BaseModel):
    label: str = Field(..., description="Focus block label (e.g. deep work).")
    start_at: str = Field(..., description="ISO datetime when focus began.")
    end_at: str = Field("", description="ISO datetime when focus ended.")
    duration_minutes: float = Field(
        0.0,
        description="Optional duration in minutes when end_at is omitted.",
    )
    note: str = ""


class LifeLogService(ServiceBase):
    _READ = ("list_metrics", "list_sleep", "list_focus")
    _MUTATE = ("log_metric", "log_sleep", "log_focus")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_metrics": action_contract(
            "core", output={"ok": "boolean", "metrics": "array"},
            errors=(("METRIC_NOT_FOUND", 404, "metric id is absent"),),
            audit=("action", "status"),
            tags=("life_log", "health", "read"),
        ),
        "list_sleep": action_contract(
            "core", output={"ok": "boolean", "sleep": "array"},
            errors=(("SLEEP_NOT_FOUND", 404, "sleep session id is absent"),),
            audit=("action", "status"),
            tags=("life_log", "sleep", "read"),
        ),
        "list_focus": action_contract(
            "core", output={"ok": "boolean", "focus": "array"},
            errors=(("FOCUS_NOT_FOUND", 404, "focus session id is absent"),),
            audit=("action", "status"),
            tags=("life_log", "focus", "read"),
        ),
        "log_metric": action_contract(
            "core", output={"ok": "boolean", "metric": "object"},
            effects=("append one health metric record",),
            audit=("action", "status", "metric_id"),
            tags=("life_log", "health", "log", "state_change"),
        ),
        "log_sleep": action_contract(
            "core", output={"ok": "boolean", "sleep": "object"},
            effects=("append one sleep session record",),
            audit=("action", "status", "sleep_id"),
            tags=("life_log", "sleep", "log", "state_change"),
        ),
        "log_focus": action_contract(
            "core", output={"ok": "boolean", "focus": "object"},
            effects=("append one focus session record",),
            audit=("action", "status", "focus_id"),
            tags=("life_log", "focus", "log", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="life_log")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "person_name": "str",
        "metrics": [{
            "id": "str",
            "metric": "str",
            "value": "float",
            "unit": "str",
            "recorded_at": "str",
            "note": "str",
        }],
        "sleep": [{
            "id": "str",
            "start_at": "str",
            "end_at": "str",
            "duration_minutes": "float",
            "quality": "enum:poor|fair|good|excellent",
            "note": "str",
        }],
        "focus": [{
            "id": "str",
            "label": "str",
            "start_at": "str",
            "end_at": "str",
            "duration_minutes": "float",
            "note": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.person_name = data.get("person_name", "")
        self.metrics: list[dict] = []
        for raw in list(data.get("metrics", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("metric"))
            entry.setdefault("metric", "")
            entry.setdefault("value", 0.0)
            entry.setdefault("unit", "")
            entry.setdefault("recorded_at", "")
            entry.setdefault("note", "")
            entry["value"] = float(entry.get("value", 0.0) or 0.0)
            self.metrics.append(entry)
        self.sleep: list[dict] = []
        for raw in list(data.get("sleep", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("sleep"))
            entry.setdefault("start_at", "")
            entry.setdefault("end_at", "")
            entry.setdefault("duration_minutes", 0.0)
            entry.setdefault("quality", "fair")
            entry.setdefault("note", "")
            entry["duration_minutes"] = float(
                entry.get("duration_minutes", 0.0) or 0.0
            )
            entry["quality"] = self._normalize_quality(entry.get("quality", "fair"))
            self.sleep.append(entry)
        self.focus: list[dict] = []
        for raw in list(data.get("focus", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("focus"))
            entry.setdefault("label", "")
            entry.setdefault("start_at", "")
            entry.setdefault("end_at", "")
            entry.setdefault("duration_minutes", 0.0)
            entry.setdefault("note", "")
            entry["duration_minutes"] = float(
                entry.get("duration_minutes", 0.0) or 0.0
            )
            self.focus.append(entry)

    def get_state(self) -> dict:
        return {
            "person_name": self.person_name,
            "metrics": self.metrics,
            "sleep": self.sleep,
            "focus": self.focus,
        }

    def _find_metric(self, metric_id: str) -> Optional[dict]:
        return next(
            (m for m in self.metrics if str(m.get("id")) == str(metric_id)),
            None,
        )

    def _find_sleep(self, sleep_id: str) -> Optional[dict]:
        return next(
            (s for s in self.sleep if str(s.get("id")) == str(sleep_id)),
            None,
        )

    def _find_focus(self, focus_id: str) -> Optional[dict]:
        return next(
            (f for f in self.focus if str(f.get("id")) == str(focus_id)),
            None,
        )

    def _filtered_metrics(self, metric: str = "") -> list[dict]:
        needle = (metric or "").strip().lower()
        items = list(self.metrics)
        if needle:
            items = [
                m for m in items
                if needle in str(m.get("metric") or "").strip().lower()
            ]
        return sorted(
            items,
            key=lambda m: (str(m.get("recorded_at") or ""), str(m.get("id") or "")),
        )

    @staticmethod
    def _normalize_quality(value: str) -> str:
        q = (value or "fair").strip().lower()
        return q if q in ("poor", "fair", "good", "excellent") else "fair"

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/metrics/list", operation_id="list_metrics",
                          summary="List recorded health metrics")
        def list_metrics(req: ListMetricsRequest):
            params = req.model_dump()
            metric_id = (req.metric_id or "").strip()
            if metric_id:
                entry = svc._find_metric(metric_id)
                if not entry:
                    svc.log_action(
                        "list_metrics",
                        params=params, status=404, metric_id=metric_id,
                    )
                    return svc.error_response(
                        code="METRIC_NOT_FOUND",
                        message="metric not found",
                        status=404,
                        details={"metric_id": metric_id},
                    )
                metrics = [entry]
            else:
                metrics = svc._filtered_metrics(req.metric)
            svc.log_action(
                "list_metrics",
                params=params,
                metric_count=len(metrics),
            )
            return {
                "ok": True,
                "person_name": svc.person_name,
                "metrics": metrics,
            }

        @self.router.post("/metrics/log", operation_id="log_metric",
                          summary="Append one health metric record")
        def log_metric(req: LogMetricRequest):
            params = req.model_dump()
            metric = (req.metric or "").strip()
            if not metric:
                svc.log_action("log_metric", params=params, status=422)
                return svc.error_response(
                    code="METRIC_REQUIRED",
                    message="metric type is required",
                    status=422,
                )
            recorded_at = (req.recorded_at or "").strip() or svc.now_iso()
            entry = {
                "id": svc.new_id("metric"),
                "metric": metric,
                "value": float(req.value),
                "unit": (req.unit or "").strip(),
                "recorded_at": recorded_at,
                "note": (req.note or "").strip(),
            }
            svc.metrics.append(entry)
            svc.log_action(
                "log_metric",
                params=params,
                effect={"metric_id": entry["id"]},
                metric_id=entry["id"],
                metric=metric,
                value=entry["value"],
            )
            return {"ok": True, "metric": entry}

        @self.router.post("/sleep/list", operation_id="list_sleep",
                          summary="List recorded sleep sessions")
        def list_sleep(req: ListSleepRequest):
            params = req.model_dump()
            sleep_id = (req.sleep_id or "").strip()
            if sleep_id:
                entry = svc._find_sleep(sleep_id)
                if not entry:
                    svc.log_action(
                        "list_sleep",
                        params=params, status=404, sleep_id=sleep_id,
                    )
                    return svc.error_response(
                        code="SLEEP_NOT_FOUND",
                        message="sleep session not found",
                        status=404,
                        details={"sleep_id": sleep_id},
                    )
                sessions = [entry]
            else:
                sessions = sorted(
                    svc.sleep,
                    key=lambda s: (
                        str(s.get("start_at") or ""),
                        str(s.get("id") or ""),
                    ),
                )
            svc.log_action(
                "list_sleep",
                params=params,
                sleep_count=len(sessions),
            )
            return {
                "ok": True,
                "person_name": svc.person_name,
                "sleep": sessions,
            }

        @self.router.post("/sleep/log", operation_id="log_sleep",
                          summary="Append one sleep session record")
        def log_sleep(req: LogSleepRequest):
            params = req.model_dump()
            start_at = (req.start_at or "").strip()
            if not start_at:
                svc.log_action("log_sleep", params=params, status=422)
                return svc.error_response(
                    code="START_REQUIRED",
                    message="sleep start_at is required",
                    status=422,
                )
            end_at = (req.end_at or "").strip()
            duration = float(req.duration_minutes or 0.0)
            if not end_at and duration <= 0:
                svc.log_action("log_sleep", params=params, status=422)
                return svc.error_response(
                    code="DURATION_REQUIRED",
                    message="provide end_at or a positive duration_minutes",
                    status=422,
                )
            entry = {
                "id": svc.new_id("sleep"),
                "start_at": start_at,
                "end_at": end_at,
                "duration_minutes": duration,
                "quality": svc._normalize_quality(req.quality),
                "note": (req.note or "").strip(),
            }
            svc.sleep.append(entry)
            svc.log_action(
                "log_sleep",
                params=params,
                effect={"sleep_id": entry["id"]},
                sleep_id=entry["id"],
            )
            return {"ok": True, "sleep": entry}

        @self.router.post("/focus/list", operation_id="list_focus",
                          summary="List recorded focus sessions")
        def list_focus(req: ListFocusRequest):
            params = req.model_dump()
            focus_id = (req.focus_id or "").strip()
            if focus_id:
                entry = svc._find_focus(focus_id)
                if not entry:
                    svc.log_action(
                        "list_focus",
                        params=params, status=404, focus_id=focus_id,
                    )
                    return svc.error_response(
                        code="FOCUS_NOT_FOUND",
                        message="focus session not found",
                        status=404,
                        details={"focus_id": focus_id},
                    )
                sessions = [entry]
            else:
                sessions = sorted(
                    svc.focus,
                    key=lambda f: (
                        str(f.get("start_at") or ""),
                        str(f.get("id") or ""),
                    ),
                )
            svc.log_action(
                "list_focus",
                params=params,
                focus_count=len(sessions),
            )
            return {
                "ok": True,
                "person_name": svc.person_name,
                "focus": sessions,
            }

        @self.router.post("/focus/log", operation_id="log_focus",
                          summary="Append one focus session record")
        def log_focus(req: LogFocusRequest):
            params = req.model_dump()
            label = (req.label or "").strip()
            start_at = (req.start_at or "").strip()
            if not label:
                svc.log_action("log_focus", params=params, status=422)
                return svc.error_response(
                    code="LABEL_REQUIRED",
                    message="focus label is required",
                    status=422,
                )
            if not start_at:
                svc.log_action("log_focus", params=params, status=422)
                return svc.error_response(
                    code="START_REQUIRED",
                    message="focus start_at is required",
                    status=422,
                )
            end_at = (req.end_at or "").strip()
            duration = float(req.duration_minutes or 0.0)
            if not end_at and duration <= 0:
                svc.log_action("log_focus", params=params, status=422)
                return svc.error_response(
                    code="DURATION_REQUIRED",
                    message="provide end_at or a positive duration_minutes",
                    status=422,
                )
            entry = {
                "id": svc.new_id("focus"),
                "label": label,
                "start_at": start_at,
                "end_at": end_at,
                "duration_minutes": duration,
                "note": (req.note or "").strip(),
            }
            svc.focus.append(entry)
            svc.log_action(
                "log_focus",
                params=params,
                effect={"focus_id": entry["id"]},
                focus_id=entry["id"],
            )
            return {"ok": True, "focus": entry}


def create_service(service_id: str) -> LifeLogService:
    return LifeLogService(service_id)
