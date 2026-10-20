from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from authorization_eva_sandbox.evaluation.post_hoc_eval import (
    resolve_vignette,
)


class PostHocVignetteResolutionTests(unittest.TestCase):
    def test_run_snapshot_vignette_has_highest_implicit_priority(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            snapshot = run_dir / "run_snapshot"
            snapshot.mkdir()
            vignette = snapshot / "vignette.json"
            vignette.write_text('{"vignette_id":"VTEST"}')
            traj = run_dir / "traj_TTEST.json"
            traj.write_text('{"task_id":"TTEST"}')
            self.assertEqual(resolve_vignette(traj, None), vignette)

    def test_bundle_local_vignette_precedes_repository_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tasks = root / "tasks"
            vignettes = root / "vignettes"
            bundle = tasks / "TTEST"
            bundle.mkdir(parents=True)
            vignettes.mkdir()
            (bundle / "task_config.json").write_text(json.dumps({
                "task_id": "TTEST",
                "source_vignette": "VTEST",
            }))
            local = bundle / "vignette.json"
            local.write_text('{"vignette_id":"VTEST"}')
            (vignettes / "VTEST.json").write_text(
                '{"vignette_id":"VTEST","fallback":true}'
            )
            run_dir = root / "run"
            run_dir.mkdir()
            traj = run_dir / "traj_TTEST.json"
            traj.write_text('{"task_id":"TTEST"}')
            with (
                patch(
                    "authorization_eva_sandbox.evaluation.bundle_paths.TASKS_DIR",
                    tasks,
                ),
                patch(
                    "authorization_eva_sandbox.evaluation.post_hoc_eval._TASKS_DIR",
                    tasks,
                ),
                patch(
                    "authorization_eva_sandbox.evaluation.post_hoc_eval._VIGNETTES_DIR",
                    vignettes,
                ),
            ):
                self.assertEqual(resolve_vignette(traj, None), local)


if __name__ == "__main__":
    unittest.main()
