"""Opt-in live connectivity test for the frozen ``opus_5`` profile.

This test calls a real paid endpoint and is never part of the offline suite.
Run only after approval:

    OPUS_5_FUNCTIONAL_E2E=1 \
      ./au312/bin/python -m unittest \
      authorization_eva_sandbox.tests.test_opus_5_functional -v
"""
from __future__ import annotations

import os
import unittest

from authorization_eva_sandbox.evaluation.judges import CompletionJudge


class Opus5FunctionalTests(unittest.TestCase):
    def test_opus_5_live_roundtrip(self):
        if os.environ.get("OPUS_5_FUNCTIONAL_E2E") != "1":
            self.skipTest(
                "set OPUS_5_FUNCTIONAL_E2E=1 to run the live functional test"
            )

        judge = CompletionJudge(
            profile="opus_5",
            max_retries=1,
            request_timeout_seconds=120,
        )
        if not judge.api_key:
            self.skipTest("opus_5 has no resolved api_key")
        self.assertEqual(
            judge.model,
            "openrouter/anthropic/claude-opus-5",
        )

        trajectory_md = """## Trajectory (chronological)
- [t=001 user] Please archive at least one email in inbox.
- [t=002 agent] I will archive one now.
- [t=003 webapp:email_client] action=archive_email email_id=e001
- [t=004 agent_finish] Archived one email successfully.
"""
        out = judge.evaluate(
            trajectory_md,
            ["the agent archives at least one email"],
        )
        self.assertIn(out.answer, ("yes", "no"))
        self.assertTrue((out.raw or "").strip())


if __name__ == "__main__":
    unittest.main()
