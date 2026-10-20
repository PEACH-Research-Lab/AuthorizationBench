"""school_directory service — student spotlight roster (v2 port of webapps/school_directory).

Fixture shape unchanged from v1 ``school_directory_data.json``:
    site_title, subtitle, intro, last_updated, people[]

Single-page read-only surface: one action loads the full roster.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class SchoolDirectoryService(ServiceBase):
    _READ = ("view_spotlights",)
    CAPABILITY_PROFILES = {
        "standard-v1": _READ,
        "full-v1": _READ,
    }
    ACTION_CONTRACTS = {
        "view_spotlights": action_contract(
            "core", output={"people": "array"},
            audit=("action", "status"),
            tags=("school_directory", "read"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="school_directory")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "site_title": "str",
        "subtitle": "str",
        "intro": "str",
        "last_updated": "str",
        "people": "list",
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.site_title = data.get("site_title", "Northfield University — Student Spotlights")
        self.subtitle = data.get("subtitle", "Spring '26 · Office of Undergraduate Research")
        self.intro = data.get("intro", "")
        self.last_updated = data.get("last_updated", "")
        self.people = list(data.get("people", []))

    def get_state(self) -> dict:
        return {"people": self.people, "site_title": self.site_title,
                "subtitle": self.subtitle, "intro": self.intro,
                "last_updated": self.last_updated}

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/spotlights", operation_id="view_spotlights",
                          summary="View the full student spotlight roster")
        def view_spotlights() -> dict:
            svc.log_action("view_spotlights", person_count=len(svc.people))
            return {
                "site_title": svc.site_title,
                "subtitle": svc.subtitle,
                "intro": svc.intro,
                "last_updated": svc.last_updated,
                "people": svc.people,
            }


def create_service(service_id: str) -> SchoolDirectoryService:
    return SchoolDirectoryService(service_id)
