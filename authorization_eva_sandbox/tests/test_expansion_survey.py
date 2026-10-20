import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion.bundle import (
    SANDBOX_ROOT,
    preflight,
)
from authorization_eva_sandbox.task_generation.expansion.originals import (
    find_original_cases,
    seeds_without_original,
)
from authorization_eva_sandbox.task_generation.expansion.run import (
    SurveyStore,
    survey_case,
)


def _case(task_id):
    case = next(c for c in find_original_cases() if c.task_id == task_id)
    if task_id == "T0005-i-g-h":
        # Pin the family as migrated from June, so expansions installed later
        # do not change what these tests expect.
        case.family = ["T0005-i-b-h", "T0005-i-b-n", "T0005-i-g-h", "T0005-i-g-n"]
    return case


def _fake_llm(case, calls):
    candidates, _covered = domain_survey.candidates_for(case)

    def llm(prompt, system, max_tokens):
        calls.append(prompt)
        if "summarize it for a domain expansion" in prompt:
            summary = {k: f"{k} text" for k in domain_survey.SUMMARY_KEYS}
            summary["current_domain"] = case.parsed.domain
            summary["domain_names"] = [{"name": "Amina Yusuf", "kind": "person", "where": "task_config.user"}]
            return summary
        return {"options": [
            {"domain": d, "feasible": d in ("h", "i"), "strength": "strong", "reason": "r",
             "new_story": "s", "plausibility": "p", "instruction_nouns": [], "services": [],
             "risks": []}
            for d in candidates
        ]}

    return llm


class OriginalCaseTests(unittest.TestCase):
    def test_every_seed_trigger_with_a_task_is_an_original(self):
        ids = {c.task_id for c in find_original_cases()}
        self.assertIn("T0034-n-g-h", ids)
        self.assertIn("T0034-f-g-h", ids)
        self.assertNotIn("T0034-n-g-n", ids)
        self.assertNotIn("T0005-i-b-h", ids)
        self.assertIn("T0005-i-g-h", ids)

    def test_seeds_without_tasks_are_listed(self):
        self.assertEqual(set(seeds_without_original()), {"S0050", "S0104"})

    def test_existing_family_domains_are_not_candidates(self):
        case = _case("T0005-i-g-h")
        candidates, covered = domain_survey.candidates_for(case)
        self.assertEqual(covered, ["b"])
        self.assertNotIn("g", candidates)
        self.assertNotIn("b", candidates)
        self.assertEqual(len(candidates), 8)


class PreflightTests(unittest.TestCase):
    def test_sealed_original_passes(self):
        self.assertEqual(preflight(SANDBOX_ROOT / "tasks" / "T0005-i-g-h"), [])

    def test_mismatched_vignette_copies_are_reported(self):
        with tempfile.TemporaryDirectory() as td:
            task = Path(td) / "tasks" / "T0005-i-g-h"
            shutil.copytree(SANDBOX_ROOT / "tasks" / "T0005-i-g-h", task)
            vignettes = Path(td) / "vignettes"
            vignettes.mkdir()
            doc = json.loads((task / "vignette.json").read_text())
            doc["narrative"] = doc["narrative"] + " changed"
            (vignettes / "V0005-i-g-h.json").write_text(json.dumps(doc))
            problems = preflight(task, vignettes_dir=vignettes)
        self.assertIn("tasks/ and vignettes/ copies of the vignette differ", problems)


class OptionValidationTests(unittest.TestCase):
    def test_missing_and_extra_domains_are_rejected(self):
        answer = {"options": [
            {k: "" for k in domain_survey.OPTION_KEYS} | {"domain": "h"},
            {k: "" for k in domain_survey.OPTION_KEYS} | {"domain": "g"},
        ]}
        issues = domain_survey.option_issues(answer, ["h", "i"])
        self.assertIn("candidate 'i' must appear exactly once", issues)
        self.assertIn("options for domains that were not asked: ['g']", issues)


class SurveyFlowTests(unittest.TestCase):
    def _run(self, answers):
        case = _case("T0005-i-g-h")
        calls = []
        replies = iter(answers)
        with tempfile.TemporaryDirectory() as td, mock.patch(
                "authorization_eva_sandbox.task_generation.expansion.run.find_original_cases",
                return_value=[case]):
            store = SurveyStore(root=Path(td))
            status = survey_case(
                case, store, _fake_llm(case, calls),
                review=True, redo=False,
                ask=lambda _prompt: next(replies), out=lambda *_a: None,
            )
            return status, store.entry(case.task_id), calls, store

    def test_accept_summary_then_pick_targets(self):
        status, entry, calls, _ = self._run(["a", "h,i"])
        self.assertEqual(status, "approved")
        self.assertEqual(entry["targets"], ["h", "i"])
        self.assertTrue(entry["summary_accepted"])
        self.assertEqual(len(calls), 2)

    def test_comment_regenerates_with_feedback(self):
        status, entry, calls, _ = self._run(["c", "name the auditor firm", "a", "none"])
        self.assertEqual(status, "none")
        self.assertEqual(entry["comments"][0]["text"], "name the auditor firm")
        self.assertIn("name the auditor firm", calls[1])
        self.assertEqual(len(calls), 3)

    def test_covered_or_unknown_letters_are_refused(self):
        status, entry, _, _ = self._run(["a", "b", "g", "i"])
        self.assertEqual(status, "approved")
        self.assertEqual(entry["targets"], ["i"])

    def test_infeasible_target_needs_confirmation(self):
        status, entry, _, _ = self._run(["a", "d", "n", "d", "y"])
        self.assertEqual(entry["targets"], ["d"])

    def test_more_targets_than_slots_are_refused(self):
        # T0005-i-g-h already has b, so four more domains fit.
        status, entry, _, _ = self._run(["a", "a,c,d,e,f", "h,i"])
        self.assertEqual(entry["targets"], ["h", "i"])


class SlotTests(unittest.TestCase):
    def test_existing_family_domains_use_slots(self):
        case = _case("T0005-i-g-h")
        self.assertEqual(domain_survey.slots_left(case), 4)
        self.assertEqual(domain_survey.slots_left(case, ["h", "i"]), 2)
        self.assertEqual(domain_survey.slots_left(case, ["h", "i", "j", "d", "e"]), 0)


if __name__ == "__main__":
    unittest.main()
