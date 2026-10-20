"""Dump the OpenHands 1.49.2 tool specs the agent sends to the model.

Run with the OpenHands interpreter, from the repository root::

    .venv-openhands/bin/python -m authorization_eva_sandbox.evaluation.harness.openai_agents.dump_openhands_tools

The adapter loads the JSON this writes. It does not import openhands.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Register the tool classes the 1.49.2 agent resolves by name.
import openhands.tools.gemini.read_file.definition  # noqa: F401
import openhands.tools.gemini.write_file.definition  # noqa: F401
import openhands.tools.terminal.definition  # noqa: F401
from openhands.sdk.tool import Tool
from openhands.sdk.tool.builtins.finish import FinishTool
from openhands.sdk.tool.registry import resolve_tool

OUTPUT = Path(__file__).with_name("openhands_tool_specs.json")
NAMES = ("terminal", "read_file", "write_file", "finish")


def _definition(name: str, working_dir: str):
    if name == "finish":
        return FinishTool.create()[0]
    # create() reads the conversation workspace. The directory must exist;
    # the description is rewritten to /workspace before the file is written.
    state = SimpleNamespace(
        workspace=SimpleNamespace(working_dir=working_dir),
        env_observation_persistence_dir=working_dir,
    )
    return resolve_tool(Tool(name=name), state)[0]  # type: ignore[arg-type]


def _without_prediction_fields(parameters: dict) -> dict:
    """Drop OpenHands' security_risk and summary. This harness does not send them."""
    properties = parameters.get("properties")
    if isinstance(properties, dict):
        for key in ("security_risk", "summary"):
            properties.pop(key, None)
    required = parameters.get("required")
    if isinstance(required, list):
        parameters["required"] = [
            name for name in required if name not in {"security_risk", "summary"}
        ]
    return parameters


def main() -> None:
    specs = []
    with tempfile.TemporaryDirectory() as working:
        for name in NAMES:
            tool = _definition(name, working)
            # Agent.step always asks for the security-risk field. This is the
            # schema that request carries, not the bare action model.
            openai_tool = tool.to_openai_tool(add_security_risk_prediction=True)
            function = openai_tool["function"]
            description = str(function["description"]).replace(working, "/workspace")
            parameters = json.loads(
                json.dumps(function["parameters"]).replace(working, "/workspace")
            )
            specs.append({
                "name": function["name"],
                "description": description,
                "parameters": _without_prediction_fields(parameters),
            })
    document = {"tools": specs}
    OUTPUT.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
