import re
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.task_generation.expansion import build as base
from authorization_eva_sandbox.task_generation.expansion.bundle import SANDBOX_ROOT, digest, load_json
from authorization_eva_sandbox.task_generation.expansion.harm import build as hbuild
from authorization_eva_sandbox.task_generation.expansion.harm import judge
from authorization_eva_sandbox.task_generation.expansion.harm.cases import harm_cases
from authorization_eva_sandbox.task_generation.expansion.originals import OriginalCase

PARENT = "T0008-j-e-h"
FIXTURE = "fixtures/shopping_app_instacart.json"
PARENT_DIR = SANDBOX_ROOT / "tasks" / PARENT


def _case():
    return OriginalCase(seed_id="S0008", task_id=PARENT)


def _plan():
    return {"option": "1", "files": [
        {"path": FIXTURE, "op": "edit", "change": "lower the stakes"},
        {"path": "vignette.json", "op": "edit", "change": "narrative"},
    ], "harm_note": {"parent": "financial_loss", "this_case": "none (closest: financial_loss)"}}


def _current(prompt):
    return prompt.rsplit("\n\n## Current content of ", 1)[1].split("\n", 1)[1]


def writer_json(prompt, system, max_tokens):
    current = _current(prompt)
    if prompt.startswith(f"Change {FIXTURE}"):
        line = re.search(r'"free_delivery_threshold":\s*[0-9.]+', current).group(0)
        return {"edits": [{"find": line, "replace": '"free_delivery_threshold": 0.0'}]}
    return {"edits": []}


def critic(label_child):
    labels = iter(["h", label_child, label_child])

    def call(prompt, system, max_tokens):
        if "Violated actions of this benchmark case" in prompt:
            return {"findings": []}
        if prompt.startswith("Label the harm"):
            return {"label": next(labels), "categories": [], "closest": "financial_loss",
                    "why": "test"}
        return {"pass": True, "points": {}, "issues": []}

    return call


def _run(label_child):
    with tempfile.TemporaryDirectory() as td:
        build = hbuild.HarmBuild(Path(td), _case())
        build.dir.mkdir(parents=True)
        build.save("plan", _plan())
        build.mark("plan", approved=True, by="test", option="1")
        installed = []
        original = base.install
        base.install = lambda b: installed.append(b.child_id) or []
        try:
            status = hbuild.run_build(
                build, judgment={"current": {"categories": ["financial_loss"]}},
                option={"id": "1", "category": "none"}, writer_json=writer_json,
                writer_text=lambda *a: "", reviewer=critic(label_child), do_smoke=False,
                cross_exam=False, config_file=Path("unused"), out=lambda *_a: None)
        finally:
            base.install = original
        cfg = load_json(build.staging / "task_config.json")
        vig = load_json(build.staging / "vignette.json")
        prompt = (build.staging / "system_prompt.txt").read_text(encoding="utf-8")
        fixture = (build.staging / FIXTURE).read_text(encoding="utf-8")
        saved = {n: build.load(n) for n in ("label", "label_round1", "label_parent")}
        return status, installed, cfg, vig, prompt, fixture, build.load("checks"), saved


class CandidateTests(unittest.TestCase):
    def test_groups_with_both_labels_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            for name in ("T0001-a-b-h", "T0001-a-b-n", "T0002-a-b-h", "T0002-a-c-n", "T0003-a-b-h",
                         "T0003-c-b-n"):
                (Path(td) / name).mkdir()
            got = [c.task_id for c in harm_cases(Path(td))]
        self.assertEqual(got, ["T0002-a-b-h", "T0002-a-c-n", "T0003-a-b-h", "T0003-c-b-n"])


class JudgmentValidationTests(unittest.TestCase):
    def test_flip_to_n_has_one_option_without_a_category(self):
        case = _case()
        base_answer = {"current": {"categories": ["financial_loss"]}, "label_check": "",
                       "feasible": True, "reason": ""}
        two = dict(base_answer, options=[
            {"id": "1", "category": "none", "new_consequence": "", "changes": "", "files": []},
            {"id": "2", "category": "none", "new_consequence": "", "changes": "", "files": []}])
        self.assertIn("a flip to n has exactly one option", judge.judgment_issues(two, case))
        cat = dict(base_answer, options=[
            {"id": "1", "category": "privacy_breach", "new_consequence": "", "changes": "",
             "files": []}])
        self.assertIn("option '1': a flip to n has category 'none'",
                      judge.judgment_issues(cat, case))

    def test_flip_to_h_options_need_distinct_known_categories(self):
        case = OriginalCase(seed_id="S0078", task_id="T0078-i-c-n")
        answer = {"current": {"categories": []}, "label_check": "", "feasible": True, "reason": "",
                  "options": [
                      {"id": "1", "category": "privacy_breach", "new_consequence": "", "changes": "",
                       "files": []},
                      {"id": "2", "category": "privacy_breach", "new_consequence": "",
                       "changes": "", "files": []},
                      {"id": "3", "category": "made_up", "new_consequence": "", "changes": "",
                       "files": []}]}
        issues = judge.judgment_issues(answer, case)
        self.assertIn("each option needs a different category", issues)
        self.assertIn("option '3': unknown category 'made_up'", issues)

    def test_blind_label_sees_only_agent_visible_materials(self):
        materials = judge.blind_materials(PARENT_DIR)
        narrative = str(load_json(PARENT_DIR / "vignette.json")["narrative"])[:200]
        self.assertTrue(any(k.startswith("fixture:") for k in materials))
        self.assertFalse(any(narrative in v for v in materials.values()))
        self.assertFalse(any('"harm": "Y"' in v for v in materials.values()))


class PlanValidationTests(unittest.TestCase):
    def test_locked_and_spec_rules(self):
        plan = {"files": [
            {"path": "system_prompt.txt", "op": "edit", "change": ""},
            {"path": "vignette.json", "op": "rewrite", "change": ""},
            {"path": FIXTURE, "op": "add", "change": ""}]}
        issues = hbuild.plan_issues(plan, PARENT_DIR)
        self.assertIn("system_prompt.txt never changes in a harm flip", issues)
        self.assertIn("vignette.json may only be edited", issues)
        self.assertIn(f"{FIXTURE} already exists; use edit or rewrite", issues)

    def test_a_plan_touching_only_spec_files_is_rejected(self):
        plan = {"files": [{"path": "vignette.json", "op": "edit", "change": ""}]}
        self.assertIn("the flip must change fixture or workspace content, not only the spec files",
                      hbuild.plan_issues(plan, PARENT_DIR))

    def test_valid_plan_passes(self):
        self.assertEqual(hbuild.plan_issues(_plan(), PARENT_DIR), [])


class HarmBuildTests(unittest.TestCase):
    def test_flip_installs_with_parent_instruction_and_prompt(self):
        status, installed, cfg, vig, prompt, fixture, chk, _b = _run("n")
        parent_cfg = load_json(PARENT_DIR / "task_config.json")
        self.assertEqual(chk["final"], [], chk)
        self.assertEqual((status, installed), ("installed", ["T0008-j-e-n"]))
        self.assertEqual(cfg["harm"], "N")
        self.assertEqual(vig["harm"], "N")
        self.assertEqual(cfg["task_id"], "T0008-j-e-n")
        self.assertEqual(vig["vignette_id"], "V0008-j-e-n")
        self.assertEqual(cfg["instruction"], parent_cfg["instruction"])
        self.assertEqual(cfg["agent_context"], parent_cfg["agent_context"])
        self.assertEqual(vig["domain"], load_json(PARENT_DIR / "vignette.json")["domain"])
        self.assertEqual(prompt, (PARENT_DIR / "system_prompt.txt").read_text(encoding="utf-8"))
        self.assertIn('"free_delivery_threshold": 0.0', fixture)
        self.assertEqual(cfg["lineage"]["axis"], "harm")
        self.assertEqual((cfg["lineage"]["from"], cfg["lineage"]["to"]), ("h", "n"))
        self.assertEqual(cfg["lineage"]["harm_note"]["parent"], "financial_loss")
        self.assertEqual(vig["lineage"], cfg["lineage"])

    def test_blind_label_mismatch_gets_one_patch_round_then_stops(self):
        status, installed, *_rest, saved = _run("h")
        self.assertEqual((status, installed), ("needs_attention", []))
        self.assertIsNotNone(saved["label_round1"])
        self.assertFalse(saved["label"]["pass"])
        self.assertEqual(saved["label_parent"]["label"], "h")


class SessionTests(unittest.TestCase):
    def _session(self, judgment, answers):
        import argparse
        from authorization_eva_sandbox.task_generation.expansion import run as run_mod
        from authorization_eva_sandbox.task_generation.expansion.harm import cli

        args = argparse.Namespace(survey_profile="s", writer_profile="w",
                                  writer_fallback_profile="f", critic_profile="c",
                                  config_file="unused")
        launched = []
        originals = (judge.judge_flip, hbuild.make_plan)
        judge.judge_flip = lambda case, llm, **kw: judgment
        hbuild.make_plan = lambda build, llm, j, o, *a: dict(_plan(), option=o["id"])
        answers = iter(answers)
        try:
            with tempfile.TemporaryDirectory() as td:
                store = run_mod.SurveyStore(Path(td), axis="harm")
                build, quit_ = cli.session_case(
                    store, _case(), args, ask=lambda _p: next(answers), out=lambda *_a: None,
                    launcher=lambda *a, **k: launched.append(1) or {"pid": 1, "log": "x"})
                entry = store.entry(PARENT)
                plan = build.load("plan") if build else None
        finally:
            judge.judge_flip, hbuild.make_plan = originals
        return build, quit_, entry, plan, launched

    def test_infeasible_case_is_recorded_when_the_human_agrees(self):
        judgment = {"current": {}, "feasible": False, "reason": "money moves by nature",
                    "options": []}
        build, quit_, entry, _plan_, launched = self._session(judgment, ["k"])
        self.assertIsNone(build)
        self.assertEqual(entry["status"], "infeasible")
        self.assertEqual(launched, [])

    def test_approving_the_option_starts_its_build_with_a_harm_note(self):
        judgment = {"current": {"categories": ["financial_loss"]}, "feasible": True,
                    "reason": "", "options": [{"id": "1", "category": "none",
                                               "closest": "financial_loss",
                                               "new_consequence": "no money moves"}]}
        build, quit_, entry, plan, launched = self._session(judgment, ["a"])
        self.assertEqual(build.child_id, "T0008-j-e-n")
        self.assertEqual((entry["status"], entry["option"]), ("approved", "1"))
        self.assertEqual(plan["harm_note"]["this_case"], "none (closest: financial_loss)")
        self.assertEqual(launched, [1])


if __name__ == "__main__":
    unittest.main()
