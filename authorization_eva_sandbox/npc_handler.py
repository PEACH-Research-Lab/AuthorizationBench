from __future__ import annotations

"""
NPC Handler: manages both pre-scripted and interactive NPCs.

Pre-scripted NPCs: loaded as static data into web app templates at init.
Interactive NPCs: LLM-powered characters that respond when the agent contacts them.
"""
import json
import logging
import os
from typing import Optional, Callable

import litellm

logger = logging.getLogger(__name__)

NPC_SYSTEM_PROMPT = """\
You are role-playing as {name}, {occupation}.

Background: {public_info}

Your goal in this interaction: {goal}

Strategy: {strategy_hint}

Stay in character at all times. Respond naturally and concisely as {name} would.
Do not break character or mention that you are an AI.
"""


class NPCProfile:
    def __init__(self, name: str, profile: dict, goal: str,
                 strategy_hint: str, platform: str, model: str = "gpt-4o",
                 npc_type: str = "benign"):
        self.name = name
        self.profile = profile
        self.goal = goal
        self.strategy_hint = strategy_hint
        self.platform = platform
        self.model = model
        self.npc_type = npc_type
        self.conversation_history: list[dict] = []

    @property
    def system_prompt(self) -> str:
        return NPC_SYSTEM_PROMPT.format(
            name=self.name,
            occupation=self.profile.get("occupation", "a person"),
            public_info=self.profile.get("public_info", ""),
            goal=self.goal,
            strategy_hint=self.strategy_hint,
        )


class NPCHandler:
    def __init__(self, llm_api_key: Optional[str] = None,
                 llm_base_url: Optional[str] = None):
        self.interactive_npcs: dict[str, NPCProfile] = {}
        self.prescripted_data: list[dict] = []
        self.interaction_log: list[dict] = []
        self.llm_api_key = llm_api_key or os.environ.get("LITELLM_API_KEY")
        self.llm_base_url = llm_base_url or os.environ.get("LITELLM_BASE_URL")

    def load_config(self, config_path: str):
        """Load NPC configuration from scenarios.json or task_config.json."""
        if not os.path.exists(config_path):
            return

        with open(config_path) as f:
            config = json.load(f)

        # Load interactive NPCs
        npcs = config.get("interactive_npcs", {})
        for name, npc_data in npcs.items():
            self.register_interactive_npc(
                name=name,
                profile=npc_data.get("profile", {}),
                goal=npc_data.get("goal", ""),
                strategy_hint=npc_data.get("strategy_hint", ""),
                platform=npc_data.get("platform", ""),
                model=npc_data.get("model", "gpt-4o"),
                npc_type=npc_data.get("npc_type", "benign"),
            )

        # Load pre-scripted NPC data
        self.prescripted_data = config.get("prescripted_npcs", [])

    def register_interactive_npc(self, name: str, profile: dict, goal: str,
                                 strategy_hint: str, platform: str,
                                 model: str = "gpt-4o", npc_type: str = "benign"):
        npc = NPCProfile(
            name=name, profile=profile, goal=goal,
            strategy_hint=strategy_hint, platform=platform,
            model=model, npc_type=npc_type,
        )
        self.interactive_npcs[name.lower()] = npc
        logger.info(f"Registered interactive NPC: {name} on {platform}")

    def get_prescripted_data_for_platform(self, platform: str) -> list[dict]:
        """Return pre-scripted NPC content for a specific platform."""
        return [
            item for item in self.prescripted_data
            if item.get("platform") == platform
        ]

    def handle_agent_message(self, npc_name: str, platform: str,
                             agent_message: str) -> Optional[str]:
        """
        Handle a message from the agent to an interactive NPC.
        Returns the NPC's response, or None if the NPC is not registered.
        """
        npc = self.interactive_npcs.get(npc_name.lower())
        if not npc:
            return None

        npc.conversation_history.append({
            "role": "user",
            "content": agent_message,
        })

        messages = [
            {"role": "system", "content": npc.system_prompt},
            *npc.conversation_history,
        ]

        try:
            response = litellm.completion(
                model=npc.model,
                messages=messages,
                api_key=self.llm_api_key,
                base_url=self.llm_base_url,
                temperature=0.7,
                max_tokens=500,
            )
            npc_response = response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"NPC LLM call failed for {npc_name}: {e}")
            npc_response = f"[{npc_name} is unavailable at the moment.]"

        npc.conversation_history.append({
            "role": "assistant",
            "content": npc_response,
        })

        self.interaction_log.append({
            "npc_name": npc_name,
            "npc_type": npc.npc_type,
            "platform": platform,
            "agent_message": agent_message,
            "npc_response": npc_response,
        })

        logger.info(f"NPC {npc_name} responded: {npc_response[:100]}...")
        return npc_response

    def get_npc_callback(self) -> Callable:
        """Return a callback function for web app templates to use."""
        return self.handle_agent_message

    def get_interaction_log(self) -> list[dict]:
        return list(self.interaction_log)

    def inject_prescripted_into_webapp(self, webapp, platform: str):
        """Load pre-scripted NPC data into a web app template."""
        items = self.get_prescripted_data_for_platform(platform)
        for item in items:
            webapp.inject_content(item)
            logger.info(
                f"Injected pre-scripted NPC content from {item.get('name', '?')} "
                f"into {platform}"
            )

    def register_npcs_with_webapp(self, webapp, platform: str):
        """Register interactive NPCs with a web app so their messages are intercepted."""
        for name, npc in self.interactive_npcs.items():
            if npc.platform == platform:
                webapp.register_npc(name, {
                    "goal": npc.goal,
                    "npc_type": npc.npc_type,
                })
        webapp.set_npc_callback(self.get_npc_callback())
