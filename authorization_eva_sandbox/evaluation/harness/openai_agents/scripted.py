"""A Model that replays scripted tool calls. Tests import this; the harness does not.

openai-agents 0.20.0 has no ``agents.testing``. Each turn is either a string
(assistant text, which ends the segment) or a list of ``{name, arguments}``
function calls.
"""
from __future__ import annotations

import json
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model, ModelTracing
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


class ScriptedModel(Model):
    def __init__(self, turns: list[Any]):
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []
        self._n = 0

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        del model_settings, output_schema, handoffs, previous_response_id, conversation_id, prompt
        names = [getattr(tool, "name", None) for tool in tools]
        self.calls.append({
            "system": system_instructions,
            "tools": names,
            "input": input,
            "tracing": tracing,
        })
        turn: Any = ""
        if self._n < len(self.turns):
            turn = self.turns[self._n]
            self._n += 1
        if isinstance(turn, BaseException):
            raise turn
        output: list[Any] = []
        if isinstance(turn, str):
            output.append(_message(turn, f"m{self._n}"))
        else:
            for index, call in enumerate(turn or []):
                output.append(ResponseFunctionToolCall(
                    arguments=json.dumps(call.get("arguments") or {}),
                    call_id=str(call.get("call_id") or f"c{self._n}_{index}"),
                    name=str(call["name"]),
                    type="function_call",
                ))
        return ModelResponse(output=output, usage=Usage(), response_id=f"r{self._n}")

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ):
        del (
            system_instructions, input, model_settings, tools, output_schema, handoffs,
            tracing, previous_response_id, conversation_id, prompt,
        )

        async def _unused():
            raise RuntimeError("the openai_agents harness does not stream")
            yield None

        return _unused()


def _message(text: str, message_id: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=message_id,
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )
