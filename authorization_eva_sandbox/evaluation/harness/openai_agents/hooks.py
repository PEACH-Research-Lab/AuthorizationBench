"""RunHooks that append one JSON line per model call and tool result."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class TrajectoryStreamHooks:
    """Duck-typed RunHooks. The SDK class is subclassed when the run starts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def bind(self) -> Any:
        from agents import RunHooks

        outer = self

        class _Hooks(RunHooks):
            async def on_llm_end(self, context, agent, response) -> None:  # noqa: ANN001
                del context, agent
                usage = getattr(response, "usage", None)
                outer._write({
                    "event": "llm_end",
                    "response_id": getattr(response, "response_id", None),
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                })

            async def on_tool_end(self, context, agent, tool, result) -> None:  # noqa: ANN001
                del context, agent
                text = result if isinstance(result, str) else str(result)
                outer._write({
                    "event": "tool_end",
                    "tool": getattr(tool, "name", None),
                    "result": text[:4000],
                })

        return _Hooks()

    def _write(self, record: dict[str, Any]) -> None:
        with self.path.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
