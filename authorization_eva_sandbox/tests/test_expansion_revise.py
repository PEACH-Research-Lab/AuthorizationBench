import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.task_generation.expansion import build as build_mod
from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion import run as run_mod
from authorization_eva_sandbox.task_generation.expansion.originals import find_original_cases

PARENT = "T0005-i-g-h"
OPTION = {"domain": "f", "feasible": True, "strength": "ok", "reason": "", "new_story": "ReelPass",
          "plausibility": "", "instruction_nouns": [], "services": [], "risks": []}


class ReviseTests(unittest.TestCase):
    def test_revise_option_keeps_the_domain(self):
        case = next(c for c in find_original_cases() if c.task_id == PARENT)
        replies = iter([dict(OPTION, domain="d"), dict(OPTION, new_story="Amazon")])
        out = domain_survey.revise_option(case, {}, OPTION, "use Amazon",
                                          lambda *a: next(replies))
        self.assertEqual((out["domain"], out["new_story"]), ("f", "Amazon"))

    def test_a_comment_rewrites_the_story_and_the_plan(self):
        case = next(c for c in find_original_cases() if c.task_id == PARENT)
        seen = {}
        originals = (domain_survey.model_llm, build_mod.make_plan)
        domain_survey.model_llm = lambda *a: (lambda *b: dict(OPTION, new_story="Amazon"))

        def fake_plan(build, llm, summary, option, feedback, previous):
            seen["story"] = option["new_story"]
            return {"files": []}

        build_mod.make_plan = fake_plan
        try:
            with tempfile.TemporaryDirectory() as td:
                store = run_mod.SurveyStore(Path(td))
                store.save(PARENT, "summary", {})
                store.save(PARENT, "options", {"options": [OPTION]})
                run_mod.revise_plan(store, case, "f", "use Amazon", writer_profile="w",
                                    fallback_profile="f", config_file=Path("unused"),
                                    out=lambda *_a: None)
                saved = store.load(PARENT, "options")["options"][0]
        finally:
            domain_survey.model_llm, build_mod.make_plan = originals
        self.assertEqual(saved["new_story"], "Amazon")
        self.assertEqual(seen["story"], "Amazon")


if __name__ == "__main__":
    unittest.main()
