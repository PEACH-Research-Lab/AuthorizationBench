"""The records page and digest show the prompt condition; older runs read as sealed."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.evaluation import run_records
from authorization_eva_sandbox.evaluation.harness.prompt_composition import prompt_fingerprint
from authorization_eva_sandbox.tools.bench_plan import _rooted

TASK = "T0099-z-z-h"
SETTING = "gpt-5.6-sol-low"


def _report(rel: str) -> str:
    return (
        "# Post-hoc\n\n## Run metadata\n\n"
        f"- **trajectory**: `{rel}`\n- **termination**: `finished`\n"
        "- **cost (provider-reported)**: $0.100000\n- **duration (s)**: 10\n"
        "- **agent iterations**: 3\n\n## Completion\n\n- **answer**: `yes`\n\n"
        "## Violation\n\n- **answer**: `no`\n"
    )


class RunRecordsPromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = Path(self._tmp.name) / "authorization_eva_sandbox"
        self.sandbox.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, harness: str, n: int, metadata: dict) -> str:
        folder = f"output_full_{TASK}_{SETTING}_{harness}_{n}"
        out = self.sandbox / "outputs" / harness / SETTING / folder
        out.mkdir(parents=True)
        (out / f"traj_{TASK}.json").write_text(json.dumps({
            "timestamp_start": 1.0 + n, "metadata": metadata, "trajectory": [],
        }))
        report = (self.sandbox / "evaluation" / "detectors" / "post_hoc_eval_report"
                  / harness / SETTING / f"{folder}.md")
        report.parent.mkdir(parents=True, exist_ok=True)
        rel = f"authorization_eva_sandbox/outputs/{harness}/{SETTING}/{folder}/traj_{TASK}.json"
        report.write_text(_report(rel))
        return folder

    def test_rows_sections_and_digest_carry_the_condition(self):
        side_effects = {"ai_disclosure": [], "memory_files": [{"path": "/workspace/AGENTS.md"}],
                        "git_identity": [], "writes_outside_workspace": []}
        sealed = self._run("openai_agents", 1, {"bundle": {"sha256": "ab" * 32}})
        native = self._run("openai_sandbox", 1, {
            "bundle": {"sha256": "ab" * 32},
            "prompt": {"condition": "native",
                       "fingerprint": prompt_fingerprint("native", "openai_sandbox")},
            "native_side_effects": side_effects,
        })
        with _rooted(self.sandbox):
            rows = {row["id"]: row for row in run_records.collect()}
            html = run_records.render(list(rows.values()))
            digest = run_records.digest(native)
            sealed_digest = run_records.digest(sealed)
        self.assertEqual((rows[sealed]["prompt"], rows[sealed]["prompt_version"]), ("sealed", "sealed"))
        self.assertEqual(rows[native]["prompt"], "native")
        self.assertEqual(rows[native]["prompt_version"], prompt_fingerprint("native", "openai_sandbox"))
        self.assertIn("OpenAI Agents SDK · prompt sealed</h2>", html)
        self.assertIn("OpenAI Agents SDK SandboxAgent · prompt native", html)
        self.assertIn("<th>prompt</th>", html)
        self.assertIn("prompt native · native side effects: memory_files 1", digest)
        self.assertIn("prompt sealed · native side effects not recorded", sealed_digest)


if __name__ == "__main__":
    unittest.main()
