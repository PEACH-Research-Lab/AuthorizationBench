from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SANDBOX = REPO_ROOT / "authorization_eva_sandbox"
DOCS = SANDBOX / "docs"


class ActiveDocumentationTests(unittest.TestCase):
    def test_only_v4_documentation_is_active(self):
        self.assertFalse((DOCS / "v2").exists())
        self.assertFalse((DOCS / "v3").exists())
        files = [
            path for path in DOCS.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        ]
        self.assertTrue(files)
        self.assertTrue(all(DOCS / "v4" in path.parents for path in files))

    def test_root_readme_has_no_retired_setup_commands(self):
        readme = (REPO_ROOT / "README.md").read_text()
        forbidden = (
            "run_eval.py",
            "openhands-ai==1.6.0",
            "builtin_loop",
            "BuiltinLoopAdapter",
        )
        for token in forbidden:
            self.assertNotIn(token, readme)

    def test_active_source_does_not_reference_removed_docs(self):
        removed = (
            "docs/v2",
            "docs/v3",
            "docs/eval-guide",
            "docs/task-layout",
            "docs/vignette",
            "docs/automated",
            "docs/webapp",
            "docs/harm",
            "docs/coding",
            "docs/model_token_price",
        )
        paths = (
            list(SANDBOX.rglob("*.py"))
            + list(SANDBOX.rglob("*.sh"))
            + [REPO_ROOT / "README.md"]
        )
        for path in paths:
            if any(part in {".git", "au312"} for part in path.parts):
                continue
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(errors="replace")
            for token in removed:
                self.assertNotIn(token, text, str(path.relative_to(REPO_ROOT)))

    def test_relative_markdown_links_resolve(self):
        markdown = [REPO_ROOT / "README.md", *sorted((DOCS / "v4").glob("*.md"))]
        pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for path in markdown:
            for raw in pattern.findall(path.read_text()):
                target = raw.strip().split("#", 1)[0]
                if (not target or target.startswith((
                    "http://", "https://", "mailto:", "/"
                ))):
                    continue
                resolved = (path.parent / target).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"{path.relative_to(REPO_ROOT)} -> {raw}",
                )

    def test_service_catalog_is_not_stored_under_docs(self):
        self.assertTrue((SANDBOX / "services/service_catalog.json").is_file())
        self.assertFalse(any(
            path.name == "service_registry.draft.json"
            for path in DOCS.rglob("*")
        ))


if __name__ == "__main__":
    unittest.main()
