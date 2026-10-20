"""user_messaging service — the Oracle User living inside the environment.

RUNTIME INFRASTRUCTURE, unlike every other template. Three deliberate
asymmetries (each enforced elsewhere; documented here so nobody "fixes" them):

- ALWAYS MOUNTED: multi_server appends it automatically, so task bundles
  never list it in ``task_config.services[]`` (Stage 4 does not emit it).
- NO FIXTURE_SCHEMA: it is config-driven — load_data reads the staged
  task_config (instruction, authorization_scope, oracle_user) via the
  TASK_CONFIG env var, never a ``{ID}_FIXTURES`` fixture file.
- NEVER A TEMPLATE SELECTION: Stage 3 hard-rejects it in carriers
  (blueprint_runtime_as_template); conversation materials bind to the
  "runtime:user_chat" carrier instead.

Exposes the frozen ``message_user`` tool under the V4 service registry:

    POST /send_message   {"message": "..."} ->
        {"delivered": true, "responded": true|false, "reply": "..."|null}

The reply is produced synchronously by the v1 Oracle policy
(``oracle_user.OracleUser``), configured from the staged (placeholder-
resolved) task_config. LLM credentials come from the LITELLM_* env vars,
which the container receives at start.

Every call is audit-logged. Clarification questions, permission requests, and
new concrete action/result reports can receive a reply; other messages are
delivered and logged with no user reply.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase
from services.contracts import action_contract

MESSAGE_USER_DESCRIPTION = (
    "Send a message to your user and return any reply they provide. Use this "
    "when you want to report progress, ask a question, or request a decision. "
    "Keep messages concise."
)


class SendMessageRequest(BaseModel):
    message: str = Field(..., description="The message to send to your user.")


class UserMessagingService(ServiceBase):
    _MUTATE = ("message_user",)
    CAPABILITY_PROFILES = {
        "standard-v1": _MUTATE,
        "full-v1": _MUTATE,
    }
    ACTION_CONTRACTS = {
        "message_user": action_contract(
            "core",
            output={
                "delivered": "boolean",
                "responded": "boolean",
                "reply": "string|null",
            },
            effects=(
                "append one outbound principal message and any synchronous Oracle reply",
            ),
            audit=("action", "status", "responded"),
            tags=("user_messaging", "oracle", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="user_messaging")
        self.oracle = None
        self._register_routes()

    def load_data(self) -> None:
        config_path = os.environ.get("TASK_CONFIG", "/config/task_config.json")
        try:
            config = json.loads(Path(config_path).read_text())
        except (OSError, json.JSONDecodeError):
            config = {}

        instruction = config.get("instruction", "")
        if isinstance(instruction, dict):
            instruction = instruction.get("instruction", "")

        from oracle_user import OracleUser

        scope = config.get("authorization_scope", {}) or {}
        oracle_cfg = config.get("oracle_user", {}) or {}
        self.oracle = OracleUser(
            violated_actions=scope.get("violated_actions", []),
            intention_clarification=scope.get("intention_clarification", ""),
            task_instruction=instruction,
            user_name=oracle_cfg.get("user_name", "the user"),
            llm_model=os.environ.get("LITELLM_MODEL", "gpt-4o"),
            llm_api_key=os.environ.get("LITELLM_API_KEY"),
            llm_base_url=os.environ.get("LITELLM_BASE_URL"),
            behavior_mode=oracle_cfg.get("behavior_mode", "natural"),
            scenario_facts=oracle_cfg.get("scripted_facts", []),
        )

    def get_state(self) -> dict:
        history = self.oracle.get_interaction_log() if self.oracle else []
        return {"interactions": history}

    def _register_routes(self) -> None:
        @self.router.post(
            "/send_message",
            summary="message_user",
            description=MESSAGE_USER_DESCRIPTION,
            operation_id="message_user",
        )
        def send_message(req: SendMessageRequest) -> dict:
            if self.oracle is None:
                self.load_data()
            reply = self.oracle.respond(req.message)
            self.log_action("message_user", params={"message": req.message},
                            reply=reply, responded=reply is not None)
            return {
                "delivered": True,
                "responded": reply is not None,
                "reply": reply,
            }


def create_service(service_id: str) -> UserMessagingService:
    return UserMessagingService(service_id)
