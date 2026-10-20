"""
WebAppTemplate: base class for all web app templates in the sandbox.

Each template is a lightweight Flask application that:
1. Serves a web UI the agent can browse
2. Loads task-specific data at initialization
3. Provides an internal API for NPC content injection
4. Records all agent actions for trajectory logging
"""
from __future__ import annotations

import json
import os
import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from flask import Flask

logger = logging.getLogger(__name__)


class WebAppTemplate(ABC):
    """
    Base class for all web app templates.
    Subclasses implement the specific platform (email, forum, shopping, etc.).
    """

    def __init__(self, template_name: str):
        self.template_name = template_name
        self.action_log: list[dict] = []
        self.registered_npcs: dict[str, dict] = {}
        self.npc_callback = None
        self.app: Optional[Flask] = None

    @abstractmethod
    def create_app(self) -> Flask:
        """Create and return the Flask application with all routes."""

    @abstractmethod
    def load_data(self, data_path: str):
        """Load task-specific data (emails, posts, products, etc.) from a JSON file."""

    def inject_content(self, content: dict):
        """
        Inject NPC response content into the app.
        Called by the NPC Handler when an interactive NPC replies.

        content should include:
        - content_type: e.g. "email", "forum_post", "chat_message"
        - data: the actual content to inject
        """
        raise NotImplementedError(
            f"inject_content not implemented for {self.template_name}"
        )

    def get_action_log(self) -> list[dict]:
        """Return all recorded agent actions."""
        return list(self.action_log)

    def log_action(self, action: str, **details):
        """Record an agent action."""
        entry = {
            "action": action,
            "timestamp": time.time(),
            "template": self.template_name,
            **details,
        }
        self.action_log.append(entry)
        logger.info(f"[{self.template_name}] Action: {action} | {details}")

    def register_npc(self, npc_name: str, npc_config: dict):
        """Register an interactive NPC so outgoing messages to them are intercepted."""
        self.registered_npcs[npc_name.lower()] = npc_config
        logger.info(f"[{self.template_name}] Registered NPC: {npc_name}")

    def set_npc_callback(self, callback):
        """
        Set callback for when the agent sends a message to a registered NPC.
        callback(npc_name, platform, agent_message) -> npc_response
        """
        self.npc_callback = callback

    def is_npc(self, name: str) -> bool:
        """Check if a name corresponds to a registered interactive NPC."""
        return name.lower() in self.registered_npcs

    def notify_npc(self, npc_name: str, agent_message: str) -> Optional[str]:
        """Notify the NPC handler that the agent has contacted an NPC."""
        if self.npc_callback and self.is_npc(npc_name):
            return self.npc_callback(npc_name, self.template_name, agent_message)
        return None

    def start(self, port: int):
        """Start the Flask web app on the specified port."""
        self.app = self.create_app()
        self.app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
