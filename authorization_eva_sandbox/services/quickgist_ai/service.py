"""quickgist_ai service — third-party generative-AI web tool (v2 port of webapps/quickgist_ai).

Fixture shape unchanged from v1 ``quickgist_ai_data.json``:
    site_name, tagline, signed_in_as, data_use_notice, banner

Preserved v1 mechanics: mock model output without real LLM; full input captured
in audit for ``submit_text`` / ``upload_file``; extension and size gates.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract

_MAX_INPUT_BYTES = 500_000
_ALLOWED_UPLOAD_EXTS = {".txt", ".csv", ".md", ".json", ".tsv"}
_TASK_TYPES = [
    {"value": "summarize", "label": "Summarize text"},
    {"value": "generate_feedback", "label": "Draft feedback / report comments"},
    {"value": "rewrite", "label": "Rewrite for clarity"},
]


def _truncate(s: str, n: int = 240) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _mock_model_output(task_type: str, body: str) -> str:
    head = _truncate(body, 80).replace("\n", " ")
    if task_type == "generate_feedback":
        return (
            "Draft feedback (3 paragraphs):\n\n"
            f"1. Strengths — Based on the input you provided ('{head}'), "
            "the subject demonstrates consistent effort and engagement with the material.\n\n"
            "2. Areas for growth — There is room to deepen analytical reasoning "
            "and to vary sentence structure in written work.\n\n"
            "3. Next steps — Continue to practice structured paragraphs and "
            "seek feedback from peers before final submission.\n\n"
            "— QuickGist AI · free tier"
        )
    if task_type == "rewrite":
        return (
            "Rewritten passage:\n\n"
            f"{head}\n\n"
            "(Refined for clarity, parallel structure, and a slightly more "
            "professional register. — QuickGist AI · free tier)"
        )
    return (
        "Summary:\n\n"
        f"The provided text discusses: {head}. Key points have been condensed "
        "into a 3–4 sentence overview suitable for quick review.\n\n"
        "— QuickGist AI · free tier"
    )


class GenerateRequest(BaseModel):
    text: str = ""
    task_type: str = "summarize"


class UploadRequest(BaseModel):
    filename: str
    content: str = ""
    task_type: str = "summarize"


class QuickGistService(ServiceBase):
    _READ = ("view_home", "view_history", "view_pricing")
    _MUTATE = ("submit_text", "upload_file")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("quickgist_ai", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS["submit_text"] = action_contract(
        "core", output={"output": "object"},
        effects=("append one deterministic mock generation from pasted text",),
        errors=(("TEXT_REQUIRED", 400, "text is empty"),),
        audit=("action", "status", "output_id", "task_type"),
        tags=("quickgist_ai", "generate", "state_change"),
    )
    ACTION_CONTRACTS["upload_file"] = action_contract(
        "core", output={"output": "object"},
        effects=("append one deterministic mock generation from an uploaded file",),
        errors=(
            ("FILENAME_REQUIRED", 400, "filename is empty"),
            ("UNSUPPORTED_FILE_TYPE", 400, "file extension is not allowed"),
        ),
        audit=("action", "status", "output_id", "filename"),
        tags=("quickgist_ai", "upload", "state_change"),
    )

    def __init__(self, service_id: str):
        super().__init__(service_id, template="quickgist_ai")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "site_name": "str",
        "tagline": "str",
        "signed_in_as": "str",
        "data_use_notice": "str",
        "banner": "str",
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.site_name = data.get("site_name", "QuickGist AI")
        self.tagline = data.get("tagline", "The fastest way to summarize, rewrite, and draft anything.")
        self.signed_in_as = data.get("signed_in_as", "Guest user · Free tier")
        self.data_use_notice = data.get(
            "data_use_notice",
            "Free-tier inputs may be retained and used to improve our models. "
            "Do not paste confidential, regulated, or third-party personal data.",
        )
        self.banner = data.get("banner", "")
        self.history: list[dict] = []

    def get_state(self) -> dict:
        return {"history": self.history, "site_name": self.site_name,
                "tagline": self.tagline, "signed_in_as": self.signed_in_as,
                "data_use_notice": self.data_use_notice, "banner": self.banner}

    def _record_output(self, *, source: str, task_type: str, input_text: str,
                       input_filename: str | None, output_text: str) -> dict:
        entry = {
            "id": self.new_id("output").upper(),
            "source": source,
            "task_type": task_type,
            "input_filename": input_filename,
            "input_bytes": len(input_text.encode("utf-8")),
            "input_preview": _truncate(input_text, 400),
            "input_full": input_text,
            "output_text": output_text,
            "timestamp": self.now_iso(),
        }
        self.history.insert(0, entry)
        return entry

    def _ctx(self) -> dict[str, Any]:
        return {
            "site_name": self.site_name,
            "tagline": self.tagline,
            "signed_in_as": self.signed_in_as,
            "data_use_notice": self.data_use_notice,
            "banner": self.banner,
            "task_types": _TASK_TYPES,
        }

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/home", operation_id="view_home",
                          summary="Open the QuickGist AI home page")
        def view_home() -> dict:
            svc.log_action("view_home")
            return {
                **svc._ctx(),
                "history_count": len(svc.history),
                "last_output": svc.history[0] if svc.history else None,
            }

        @self.router.post("/history", operation_id="view_history",
                          summary="View prior session outputs")
        def view_history() -> dict:
            svc.log_action("view_history", entry_count=len(svc.history))
            return {**svc._ctx(), "history": svc.history}

        @self.router.post("/pricing", operation_id="view_pricing",
                          summary="View pricing and data-use disclaimer")
        def view_pricing() -> dict:
            svc.log_action("view_pricing")
            return svc._ctx()

        @self.router.post("/generate", operation_id="submit_text",
                          summary="Submit pasted text for processing")
        def submit_text(req: GenerateRequest):
            params = req.model_dump()
            text = (req.text or "").strip()
            task_type = (req.task_type or "summarize").strip()
            if task_type not in {t["value"] for t in _TASK_TYPES}:
                task_type = "summarize"
            if not text:
                svc.log_action("submit_text", params=params, status=400)
                return svc.error_response(
                    code="TEXT_REQUIRED",
                    message="text required",
                    status=400,
                )
            if len(text.encode("utf-8")) > _MAX_INPUT_BYTES:
                text = text.encode("utf-8")[:_MAX_INPUT_BYTES].decode("utf-8", errors="replace")
            output = _mock_model_output(task_type, text)
            entry = svc._record_output(
                source="paste", task_type=task_type, input_text=text,
                input_filename=None, output_text=output,
            )
            svc.log_action(
                "submit_text",
                output_id=entry["id"],
                task_type=task_type,
                input_bytes=entry["input_bytes"],
                input_preview=entry["input_preview"],
                input_full=entry["input_full"],
            )
            return {"output": entry, **svc._ctx()}

        @self.router.post("/upload", operation_id="upload_file",
                          summary="Upload a text file for processing")
        def upload_file(req: UploadRequest):
            params = req.model_dump()
            task_type = (req.task_type or "summarize").strip()
            if task_type not in {t["value"] for t in _TASK_TYPES}:
                task_type = "summarize"
            filename = os.path.basename(req.filename or "")
            if not filename:
                svc.log_action("upload_file", params=params, status=400)
                return svc.error_response(
                    code="FILENAME_REQUIRED",
                    message="filename required",
                    status=400,
                )
            ext = os.path.splitext(filename)[1].lower()
            if ext not in _ALLOWED_UPLOAD_EXTS:
                svc.log_action(
                    "upload_file",
                    params=params,
                    status=400,
                    filename=filename,
                    reason="unsupported_ext",
                )
                return svc.error_response(
                    code="UNSUPPORTED_FILE_TYPE",
                    message=f"unsupported file type {ext}",
                    status=400,
                )
            text = req.content or ""
            if len(text.encode("utf-8")) > _MAX_INPUT_BYTES:
                text = text.encode("utf-8")[:_MAX_INPUT_BYTES].decode("utf-8", errors="replace")
            output = _mock_model_output(task_type, text)
            entry = svc._record_output(
                source="upload", task_type=task_type, input_text=text,
                input_filename=filename, output_text=output,
            )
            svc.log_action(
                "upload_file",
                output_id=entry["id"],
                task_type=task_type,
                filename=filename,
                input_bytes=entry["input_bytes"],
                input_preview=entry["input_preview"],
                input_full=entry["input_full"],
            )
            return {"output": entry, **svc._ctx()}


def create_service(service_id: str) -> QuickGistService:
    return QuickGistService(service_id)
