"""saved_info service — the agent's long-term-memory surface (v2 port of webapps/saved_info).

Fixture shape unchanged from v1: app_name, title, description, user_name,
memories[{id, text, source, confirmed, created_at}].
"""
from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class AddMemoryRequest(BaseModel):
    text: str = Field(..., description="The fact to save for future sessions.")
    source: str = "agent"
    confirmed: bool = Field(False, description="Whether the user confirmed saving this.")


class SavedInfoService(ServiceBase):
    _READ = ("list_saved_info",)
    _MUTATE = ("add_saved_info",)
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_saved_info": action_contract(
            "core", output={"memories": "array"},
            audit=("action", "status"),
            tags=("saved_info", "read"),
        ),
        "add_saved_info": action_contract(
            "core", output={"ok": "boolean", "memory": "object"},
            effects=("append one long-term memory fact",),
            errors=(("MEMORY_TEXT_REQUIRED", 400, "memory text is empty"),),
            audit=("action", "status"),
            tags=("saved_info", "memory", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="saved_info")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "app_name": "str",
        "title": "str",
        "description": "str",
        "user_name": "str",
        "memories": [{"id": "str", "text": "str", "source": "str",
                      "confirmed": "bool", "created_at": "str"}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.app_name = data.get("app_name", "Saved Info")
        self.title = data.get("title", "Saved Info")
        self.description = data.get(
            "description", "Long-term facts the assistant may use in future sessions.")
        self.user_name = data.get("user_name", "User")
        self.memories = list(data.get("memories", []))

    def get_state(self) -> dict:
        return {"memories": self.memories, "app_name": self.app_name,
                "title": self.title, "description": self.description,
                "user_name": self.user_name}

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/memories/list", operation_id="list_saved_info",
                          summary="List all saved long-term facts about the user")
        def list_memories() -> dict:
            svc.log_action("list_saved_info", params={},
                           memory_count=len(svc.memories))
            return {"app_name": svc.app_name, "description": svc.description,
                    "memories": svc.memories}

        @self.router.post("/memories/add", operation_id="add_saved_info",
                          summary="Save a new long-term fact about the user")
        def add_memory(req: AddMemoryRequest):
            params = req.model_dump()
            text = req.text.strip()
            if not text:
                svc.log_action("add_saved_info", params=params, status=400)
                return svc.error_response(
                    code="MEMORY_TEXT_REQUIRED",
                    message="Memory text is required.",
                    status=400,
                )
            memory = {
                "id": svc.new_id("mem"),
                "text": text,
                "source": req.source.strip() or "agent",
                "confirmed": req.confirmed,
                "created_at": svc.now_iso(),
            }
            svc.memories.append(memory)
            svc.log_action("add_saved_info", params=params, **memory)
            return {"ok": True, "memory": memory}


def create_service(service_id: str) -> SavedInfoService:
    return SavedInfoService(service_id)
