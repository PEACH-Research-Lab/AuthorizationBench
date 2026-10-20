from __future__ import annotations

"""
School Directory — Northfield Student Spotlights.

A single-page Flask app that renders student "spotlight" cards in
narrative form. Each card shows:
  - an emoji avatar in a colored circle
  - the student's display name
  - a free-form prose paragraph (the student's own write-up) —
    every paragraph is in a different voice (first/third person,
    formal/casual, listy/poetic) and weaves the student's school,
    department, major, year of study, and hobbies into the prose
    with NO structural key:value labels.

There is no per-student URL and no edit endpoint — the agent loads
"/" once and gets the whole class roster as a long page of cards.
"""
import json
import os
import sys

from flask import Flask, render_template

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


class SchoolDirectoryApp(WebAppTemplate):
    def __init__(self):
        super().__init__("school_directory")
        self.site_title: str = "Northfield University — Student Spotlights"
        self.subtitle: str = "Spring '26 · Office of Undergraduate Research"
        self.intro: str = ""
        self.last_updated: str = ""
        self.people: list[dict] = []

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.site_title = data.get("site_title", self.site_title)
        self.subtitle = data.get("subtitle", self.subtitle)
        self.intro = data.get("intro", self.intro)
        self.last_updated = data.get("last_updated", self.last_updated)
        self.people = list(data.get("people", []))

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        directory = self

        @app.route("/")
        def index():
            directory.log_action(
                "view_spotlights", person_count=len(directory.people)
            )
            return render_template(
                "index.html",
                site_title=directory.site_title,
                subtitle=directory.subtitle,
                intro=directory.intro,
                last_updated=directory.last_updated,
                people=directory.people,
            )

        @app.route("/api/action_log")
        def action_log():
            return {"actions": directory.get_action_log()}

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5010"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    directory = SchoolDirectoryApp()
    directory.load_data(data_path)
    directory.start(port)


if __name__ == "__main__":
    main()
