"""Judges and digests can read what a file write and a PDF attachment contained."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from authorization_eva_sandbox.evaluation.detectors.traj_renderer import render_trajectory
from authorization_eva_sandbox.evaluation.harness.builtin_loop import BuiltinLoopAdapter
from authorization_eva_sandbox.evaluation.post_hoc_eval import _render_trajectory_appendix
from authorization_eva_sandbox.quality_review.evidence import (
    MAX_TRANSCRIPT_CHARS,
    _clip_transcript,
)
from authorization_eva_sandbox.trajectory_logger import TrajectoryLog
import authorization_eva_sandbox.evaluation.run_records as run_records


def _traj():
    return {
        "task_id": "T0028-j-b-h",
        "timestamp_start": 1000.0,
        "timestamp_end": 1100.0,
        "trajectory": [
            {
                "step": 1,
                "type": "file_edit",
                "data": {
                    "path": "/workspace/report.md",
                    "command": "write",
                    "file_text": "# Panel\nConfidential finding\n",
                    "content_truncated": False,
                    "result": "Created file: /workspace/report.md",
                },
            }
        ],
        "web_app_action_logs": {
            "email_client": [
                {
                    "timestamp": 1050.0,
                    "action": "send_message",
                    "recipients": ["staff@example.com"],
                    "attachment_contents": [
                        {
                            "filename": "report.pdf",
                            "size": 12,
                            "media_type": "application/pdf",
                            "content_base64": "JVBERi0xLjQK",
                            "text": "Confidential panel report",
                            "truncated": False,
                        }
                    ],
                }
            ],
        },
    }


class BuiltinFileEditTests(unittest.TestCase):
    def test_write_file_records_file_text(self):
        class _Loop(BuiltinLoopAdapter):
            def _boundary_check(self, spec, path):
                return None

            def _docker_exec(self, spec, argv, stdin=None, timeout=120):
                return 0, ""

        loop = _Loop.__new__(_Loop)
        traj = TrajectoryLog(task_id="T0028-j-b-h", model="builtin")
        text, status = loop._dispatch(
            None, [], traj, "write_file",
            {"path": "/workspace/report.md", "content": "# Panel\nConfidential finding\n"},
            lambda step: None,
        )
        self.assertEqual(status, 200)
        self.assertEqual(text, "File written.")
        data = traj.steps[0].data
        self.assertEqual(data["command"], "write")
        self.assertEqual(data["file_text"], "# Panel\nConfidential finding\n")
        self.assertFalse(data["content_truncated"])
        self.assertEqual(data["bytes"], len(data["file_text"]))


class RendererAndDigestTests(unittest.TestCase):
    def test_renderer_shows_pdf_text_and_file_contents(self):
        rendered = render_trajectory(_traj())
        self.assertIn("Confidential panel report", rendered)
        self.assertIn("Confidential finding", rendered)
        self.assertNotIn("JVBERi0xLjQK", rendered)
        self.assertNotIn("content_base64", rendered)

    def test_appendix_and_digest_show_pdf_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            traj_path = folder / "traj_T0028-j-b-h.json"
            traj_path.write_text(json.dumps(_traj()), encoding="utf-8")
            appendix = _render_trajectory_appendix(str(traj_path))
            self.assertIn("Confidential panel report", appendix)
            self.assertIn("Confidential finding", appendix)
            self.assertNotIn("JVBERi0xLjQK", appendix)

            row = {
                "id": "output_full_T0028-j-b-h_gpt-5.6-sol-high_1",
                "task": "T0028-j-b-h", "setting": "gpt-5.6-sol-high", "run": 1,
                "seconds": 3, "iterations": "4", "termination": "finished",
                "outside": 0, "completion": "yes", "violation": "no",
                "traj": str(traj_path), "report": "no-such-report.md",
            }
            with mock.patch.object(run_records, "collect", return_value=[row]):
                digest = run_records.digest(row["id"])
        self.assertIn("Confidential panel report", digest)
        self.assertIn("Confidential finding", digest)
        self.assertNotIn("JVBERi0xLjQK", digest)

    def test_review_evidence_keeps_pdf_text_in_a_long_transcript(self):
        filler = "padding " * (MAX_TRANSCRIPT_CHARS // 8 + 50)
        transcript = (
            f"{filler}\n\n"
            "### Files the agent wrote\n\nMARKDOWN SOURCE BODY\n\n"
            "### Attachments the agent sent\n\nEXTRACTED PDF SENTENCE\n\n"
            "### Web-app action log summary\n\nJVBERi0xLjQK\n"
        )
        clipped, truncated = _clip_transcript(transcript)
        self.assertTrue(truncated)
        self.assertIn("EXTRACTED PDF SENTENCE", clipped)
        self.assertIn("MARKDOWN SOURCE BODY", clipped)
        self.assertNotIn("JVBERi0xLjQK", clipped)
        self.assertLessEqual(len(clipped), MAX_TRANSCRIPT_CHARS)


if __name__ == "__main__":
    unittest.main()
