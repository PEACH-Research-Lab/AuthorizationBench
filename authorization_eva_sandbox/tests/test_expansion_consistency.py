import json
import shutil
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.task_generation.expansion import checks
from authorization_eva_sandbox.task_generation.expansion.bundle import SANDBOX_ROOT, load_json

SOURCE = SANDBOX_ROOT / "tasks" / "T0008-j-e-h"


class ConsistencyIssuesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.task = self.tmp / SOURCE.name
        shutil.copytree(SOURCE, self.task, ignore=shutil.ignore_patterns("_generation"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _edit(self, name, change):
        path = self.task / name
        data = load_json(path)
        change(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _issues(self):
        return [i["issue"] for i in checks.consistency_issues(self.task)]

    def test_clean_bundle_has_no_issues(self):
        self.assertEqual(self._issues(), [])

    def test_runtime_only_oracle_keys_are_ignored(self):
        self._edit("task_config.json",
                   lambda c: c["oracle_user"].update(scripted_facts=[{"q": "x", "a": "y"}]))
        self.assertEqual(self._issues(), [])

    def test_policy_drift_between_task_config_and_vignette(self):
        self._edit("vignette.json", lambda v: v["oracle_user"].update(user_name="Someone Else"))
        self.assertTrue(any(i.startswith("oracle_user differs") for i in self._issues()))

    def test_design_path_missing_from_materials(self):
        self._edit("vignette.json", lambda v: v.update(
            narrative=v["narrative"] + " Her home is `/home/zed/projects/side`."))
        self.assertTrue(any("/home/zed/projects/side" in i for i in self._issues()))

    def test_design_path_present_in_materials_is_fine(self):
        workspace = self.task / "workspace" / "notes"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "todo.md").write_text("buy milk\n", encoding="utf-8")
        self._edit("vignette.json", lambda v: v.update(
            narrative=v["narrative"] + " She keeps /workspace/notes/todo.md."))
        self.assertEqual(self._issues(), [])

    def test_self_possessive_name(self):
        self._edit("vignette.json", lambda v: v.update(
            narrative="Maya Chen is the fieldworker at Maya Chen's fieldwork project. "
                      + v["narrative"]))
        self.assertTrue(any("own employer" in i for i in self._issues()))

    def test_review_materials_carries_design_and_every_visible_text(self):
        bundle = checks.review_materials(self.task)
        self.assertIn("authorization_scope", bundle["design"])
        self.assertIn("pressure_placement", bundle["design"])
        self.assertIn("instruction", bundle["materials"])
        self.assertTrue(all(len(t) <= 4000 + len("\n...(cut)") for t in bundle["materials"].values()))


class PromptMemorySyncTests(unittest.TestCase):
    PARENT = SANDBOX_ROOT / "tasks" / "T0004-b-f-h"

    def test_memory_edit_reaches_the_system_prompt(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            child = tmp / "child"
            shutil.copytree(self.PARENT, child, ignore=shutil.ignore_patterns("_generation"))
            mem = child / "prior_memory.md"
            line = "- On June 6, Elena backed up every file on this workstation to the studio NAS."
            mem.write_text(mem.read_text(encoding="utf-8").rstrip() + "\n\n" + line + "\n",
                           encoding="utf-8")
            self.assertIsNotNone(checks.prompt_memory_issue(self.PARENT, child))
            checks.sync_prompt_memory(self.PARENT, child)
            prompt = (child / "system_prompt.txt").read_text(encoding="utf-8")
            self.assertIn(line, prompt)
            self.assertIn("<TOOLS>", prompt)
            self.assertIsNone(checks.prompt_memory_issue(self.PARENT, child))
        finally:
            shutil.rmtree(tmp)

    def test_prompt_copy_with_an_extra_line_is_flagged_and_rebuilt(self):
        parent = SANDBOX_ROOT / "tasks" / "T0002-f-a-h"
        tmp = Path(tempfile.mkdtemp())
        try:
            child = tmp / "child"
            shutil.copytree(parent, child, ignore=shutil.ignore_patterns("_generation"))
            prompt = child / "system_prompt.txt"
            text = prompt.read_text(encoding="utf-8")
            prompt.write_text(text.replace("</PRIOR_MEMORY>",
                                           "- She was still hoping to join the lab.\n</PRIOR_MEMORY>", 1),
                              encoding="utf-8")
            self.assertIsNotNone(checks.prompt_memory_issue(parent, child))
            checks.sync_prompt_memory(parent, child)
            self.assertEqual(prompt.read_text(encoding="utf-8"), text)
            self.assertIsNone(checks.prompt_memory_issue(parent, child))
        finally:
            shutil.rmtree(tmp)

    def test_unchanged_memory_leaves_prompt_alone(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            child = tmp / "child"
            shutil.copytree(self.PARENT, child, ignore=shutil.ignore_patterns("_generation"))
            before = (child / "system_prompt.txt").read_bytes()
            checks.sync_prompt_memory(self.PARENT, child)
            self.assertEqual((child / "system_prompt.txt").read_bytes(), before)
        finally:
            shutil.rmtree(tmp)


class AdvisoryReviewTests(unittest.TestCase):
    def test_only_advisory_points_failed_passes(self):
        review = checks.with_advisory({"pass": False, "points": {
            "boundary_visible": {"ok": False, "note": "only in the narrative"},
            "workflow_kept": {"ok": True, "note": ""}}, "issues": []})
        self.assertTrue(review["pass"])
        self.assertEqual(review["advisory"][0]["point"], "boundary_visible")

    def test_other_failed_point_still_fails(self):
        review = checks.with_advisory({"pass": False, "points": {
            "pressure_present": {"ok": False, "note": "no banner"},
            "workflow_kept": {"ok": False, "note": "step dropped"}}, "issues": []})
        self.assertFalse(review["pass"])
        self.assertEqual([a["point"] for a in review["advisory"]], ["pressure_present"])

    def test_preflight_does_not_block_on_notes(self):
        from authorization_eva_sandbox.task_generation.expansion.bundle import preflight
        tmp = Path(tempfile.mkdtemp())
        try:
            task = tmp / SOURCE.name
            shutil.copytree(SOURCE, task, ignore=shutil.ignore_patterns("_generation"))
            path = task / "vignette.json"
            data = load_json(path)
            data["narrative"] += " Her home is `/home/zed/projects/side`."
            text = json.dumps(data, ensure_ascii=False, indent=2)
            path.write_text(text, encoding="utf-8")
            vignettes = tmp / "vignettes"
            vignettes.mkdir()
            (vignettes / ("V" + SOURCE.name[1:] + ".json")).write_text(text, encoding="utf-8")
            self.assertTrue(checks.consistency_issues(task))
            self.assertEqual(preflight(task, vignettes_dir=vignettes), [])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
