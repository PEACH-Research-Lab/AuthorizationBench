"""Queue behavior for four trigger-flip workers."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from authorization_eva_sandbox.task_generation.expansion import trigger_flip_batch as batch


class QueueTests(unittest.TestCase):
    def test_pop_claims_each_task_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue.txt"
            queue.write_text("T0001-c-a-h\nT0002-i-b-h\n")
            with (
                mock.patch.object(batch, "QUEUE", queue),
                mock.patch.object(batch, "QUEUE_LOCK", root / "queue.lock"),
            ):
                self.assertEqual(batch._pop(), "T0001-c-a-h")
                self.assertEqual(batch._pop(), "T0002-i-b-h")
                self.assertIsNone(batch._pop())

    def test_record_is_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            done = root / "done.jsonl"
            with (
                mock.patch.object(batch, "DONE", done),
                mock.patch.object(batch, "DONE_LOCK", root / "done.lock"),
            ):
                batch._record(2, "T0005-i-b-h", "installed")
            text = done.read_text()
        self.assertIn('"slot": 2', text)
        self.assertIn('"destination": "T0005-n-b-h"', text)


if __name__ == "__main__":
    unittest.main()

