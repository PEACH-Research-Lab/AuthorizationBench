import argparse
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.task_generation.expansion import build as build_mod
from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion import session
from authorization_eva_sandbox.task_generation.expansion.bundle import load_json
from authorization_eva_sandbox.task_generation.expansion.originals import find_original_cases
from authorization_eva_sandbox.task_generation.expansion.run import SurveyStore

PARENT = "T0005-i-g-h"


def _case():
    case = next(c for c in find_original_cases() if c.task_id == PARENT)
    # Pin the family as migrated from June; later installs must not change the test.
    case.family = ["T0005-i-b-h", "T0005-i-b-n", "T0005-i-g-h", "T0005-i-g-n"]
    return case


def _args():
    return argparse.Namespace(survey_profile="s", writer_profile="w",
                              writer_fallback_profile="wf", critic_profile="c",
                              config_file="unused.toml")


def _option(letter, feasible=True):
    return {"domain": letter, "feasible": feasible, "strength": "strong", "reason": "r",
            "new_story": "story", "plausibility": "p", "instruction_nouns": [],
            "services": [], "risks": []}


def _plan(parent_dir):
    cfg = load_json(parent_dir / "task_config.json")
    files = [{"path": f["path"], "op": "rewrite" if f["text"] else "keep", "new_path": "",
              "change": "swap"} for f in build_mod.parent_file_list(parent_dir)]
    return {"instruction": cfg["instruction"], "entity_map": [], "files": files,
            "snapshot_roots": [], "notes": ""}


def _prepared_store(root):
    store = SurveyStore(root=root)
    case = _case()
    summary = {k: "x" for k in domain_survey.SUMMARY_KEYS}
    summary["current_domain"] = "g"
    store.save(case.task_id, "summary", summary)
    store.save(case.task_id, "options", {
        "options": [_option("h"), _option("i"), _option("a", feasible=False)],
        "covered": ["b"], "candidates": ["a", "h", "i"],
    })
    for letter in ("h", "i"):
        build = build_mod.Build(root, case, letter)
        build.dir.mkdir(parents=True)
        build.save("plan", _plan(build.parent_dir))
        build.mark("plan", approved=False)
    return store, case


class SessionTests(unittest.TestCase):
    def test_approve_starts_a_build_and_reject_records_it(self):
        with tempfile.TemporaryDirectory() as td:
            store, case = _prepared_store(Path(td))
            launched = []

            def launcher(store_, case_, target, args, window):
                launched.append(target)
                return {"pid": 0, "log": "build.log", "window": False}

            replies = iter(["a", "r", "not natural"])
            started, quit_ = session.run_session(
                store, case, _args(), ask=lambda _p: next(replies),
                out=lambda *_a: None, launcher=launcher, window=False)
            entry = store.entry(case.task_id)
            self.assertFalse(quit_)
            self.assertEqual(launched, ["h"])
            self.assertEqual([b.child_id for b in started], ["T0005-i-h-h"])
            self.assertEqual(entry["targets"], ["h"])
            self.assertEqual(entry["rejected"], ["i"])
            self.assertTrue(build_mod.Build(Path(td), case, "h").state()["steps"]["plan"]["approved"])

    def test_quit_stops_without_launching(self):
        with tempfile.TemporaryDirectory() as td:
            store, case = _prepared_store(Path(td))
            started, quit_ = session.run_session(
                store, case, _args(), ask=lambda _p: "q", out=lambda *_a: None,
                launcher=lambda *a, **k: self.fail("should not launch"), window=False)
            self.assertTrue(quit_)
            self.assertEqual(started, [])


class StatusTests(unittest.TestCase):
    def test_finished_build_reports_smoke_and_diagnosis(self):
        with tempfile.TemporaryDirectory() as td:
            store, case = _prepared_store(Path(td))
            build = build_mod.Build(Path(td), case, "h")
            for step in ("files",):
                build.mark(step)
            build.save("checks", {"final": []})
            build.mark("checks", passed=True)
            build.save("leak", {"pass": True})
            build.mark("leak")
            build.save("review", {"pass": True})
            build.mark("review")
            build.mark("install", problems=[])
            build.mark("smoke", result={"completion": "yes", "violation": "no"})
            build.mark("diagnosis", verdict="model_chose_safely")
            rows = [session.build_status(b) for b in session.all_builds(store)]
            by_child = {r["child"]: r for r in rows}
            row = by_child["T0005-i-h-h"]
            self.assertEqual(row["state"], "done")
            self.assertEqual((row["violation"], row["diagnosis"]), ("no", "model_chose_safely"))
            self.assertEqual(by_child["T0005-i-i-h"]["state"], "plan awaiting approval")
            self.assertIn("T0005-i-h-h", session.render_status(rows))

    def test_watch_returns_when_nothing_is_running(self):
        with tempfile.TemporaryDirectory() as td:
            store, case = _prepared_store(Path(td))
            rows = session.watch(session.all_builds(store), out=lambda *_a: None,
                                 sleep=lambda _s: self.fail("should not wait"))
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
