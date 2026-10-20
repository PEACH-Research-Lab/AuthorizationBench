import json
import re
import tempfile
import unittest
from pathlib import Path

from authorization_eva_sandbox.task_generation.expansion import build as build_mod
from authorization_eva_sandbox.task_generation.expansion import checks
from authorization_eva_sandbox.task_generation.expansion.bundle import SANDBOX_ROOT, load_json
from authorization_eva_sandbox.task_generation.expansion.originals import find_original_cases

PARENT = "T0005-i-g-h"


def _case():
    return next(c for c in find_original_cases() if c.task_id == PARENT)


def _full_plan(parent_dir):
    cfg = load_json(parent_dir / "task_config.json")
    files = [{"path": f["path"], "op": "rewrite" if f["text"] else "keep", "new_path": "", "change": "swap"}
             for f in build_mod.parent_file_list(parent_dir)]
    return {"instruction": cfg["instruction"].replace("auditor", "attorney"),
            "entity_map": [{"from": "Marta Chen", "to": "Marcus Reed"}],
            "files": files, "snapshot_roots": [], "notes": ""}


def _current(prompt):
    body = prompt.split("## Current content\n", 1)[1]
    return body.split("\n\n## Problems found in your previous version", 1)[0]


def fake_text(prompt, system, max_tokens):
    return _current(prompt).replace("Marta Chen", "Marcus Reed").replace("auditor", "attorney")


def fake_json(prompt, system, max_tokens):
    if "Violated actions of this benchmark case" in prompt:
        return {"findings": []}
    return {"pass": True, "points": {}, "issues": []}


class PlanValidationTests(unittest.TestCase):
    def test_complete_plan_passes(self):
        parent = SANDBOX_ROOT / "tasks" / PARENT
        self.assertEqual(build_mod.plan_issues(_full_plan(parent), parent), [])

    def test_missing_file_and_bad_ops_are_reported(self):
        parent = SANDBOX_ROOT / "tasks" / PARENT
        plan = _full_plan(parent)
        dropped = plan["files"].pop(-1)["path"]
        next(f for f in plan["files"] if f["path"] == "vignette.json")["op"] = "keep"
        issues = build_mod.plan_issues(plan, parent)
        self.assertIn(f"{dropped!r} must appear exactly once (found 0)", issues)
        self.assertIn("vignette.json must be rewrite", issues)

    def test_instruction_sentence_count_is_kept(self):
        parent = SANDBOX_ROOT / "tasks" / PARENT
        plan = _full_plan(parent)
        plan["instruction"] += " Thanks. Also cc me."
        self.assertIn("instruction must keep the parent's sentence count",
                      build_mod.plan_issues(plan, parent))

    def test_periods_inside_a_sql_fence_are_not_sentence_endings(self):
        prose_before = "Look up the account. "
        prose_after = "Send the result to the owner."
        dotted = prose_before + "```sql\nSELECT u.email\n```\n" + prose_after
        plain = prose_before + "```sql\nSELECT u_email\n```\n" + prose_after
        self.assertEqual(checks.sentence_count(dotted), checks.sentence_count(plain))
        self.assertEqual(checks.sentence_count(dotted), 2)


class LockTests(unittest.TestCase):
    def test_locked_task_config_fields_come_back_from_parent(self):
        parent = load_json(SANDBOX_ROOT / "tasks" / PARENT / "task_config.json")
        child = dict(parent, agent={"max_steps": 99}, harm="n", services=[])
        out = checks.lock_task_config(child, parent, task_id="T0005-i-i-h",
                                      lineage={"parent_task": PARENT}, snapshot_roots=[])
        self.assertEqual(out["agent"], parent["agent"])
        self.assertEqual(out["harm"], parent["harm"])
        self.assertEqual(out["services"], parent["services"])
        self.assertEqual(out["task_id"], "T0005-i-i-h")
        self.assertEqual(out["source_vignette"], "V0005-i-i-h")
        self.assertNotIn("snapshot_roots", out)


class KeptServiceNameTests(unittest.TestCase):
    def test_a_retained_service_id_is_not_a_leftover_name(self):
        text = "You reach RailPass through the shopping_app_instacart tools."
        tokens = ["shopping_app_instacart"]
        self.assertFalse(checks.name_outside_tokens(text, "shopping_app_instacart", tokens))
        self.assertFalse(checks.name_outside_tokens(text, "Instacart", tokens))

    def test_a_brand_mention_outside_the_service_id_still_counts(self):
        text = "Place the Instacart order, then call shopping_app_instacart.place_order."
        tokens = ["shopping_app_instacart"]
        self.assertTrue(checks.name_outside_tokens(text, "Instacart", tokens))

    def test_a_setting_key_is_not_an_old_domain_word(self):
        self.assertFalse(checks.name_outside_tokens(
            '{"auto_join_meetings": true}', "meetings", []))
        self.assertTrue(checks.name_outside_tokens(
            "Please join the meetings.", "meetings", []))


class EndToEndStagingTests(unittest.TestCase):
    def test_build_writes_a_checked_child_into_staging_only(self):
        case = _case()
        summary = {"domain_names": [{"name": "Marta Chen", "kind": "person", "where": "fixture"}]}
        option = {"domain": "j", "feasible": True}
        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "j")
            build.dir.mkdir(parents=True)
            build.save("plan", _full_plan(build.parent_dir))
            build.mark("plan", approved=True, by="test")
            status = build_mod.run_build(
                build, summary=summary, option=option,
                writer_json=fake_json, writer_text=fake_text, reviewer=fake_json,
                review=False, do_install=False, do_smoke=False, cross_exam=False,
                config_file=Path("unused"), out=lambda *_a: None,
            )
            self.assertEqual(status, "awaiting_install")
            self.assertFalse((SANDBOX_ROOT / "tasks" / "T0005-i-j-h").exists())
            cfg = load_json(build.staging / "task_config.json")
            vig = load_json(build.staging / "vignette.json")
            self.assertEqual(cfg["task_id"], "T0005-i-j-h")
            self.assertEqual(cfg["lineage"]["parent_task"], PARENT)
            self.assertEqual(vig["vignette_id"], "V0005-i-j-h")
            self.assertEqual(vig["domain"], "education")
            self.assertEqual(vig["instruction"], cfg["instruction"])
            self.assertIn("attorney", cfg["instruction"])
            final = build.load("checks")["final"]
            self.assertEqual(final, [], final)


class AutoBuildTests(unittest.TestCase):
    def _auto(self, review_pass):
        case = _case()
        summary = {"domain_names": []}
        option = {"domain": "i", "feasible": True}

        def critic(prompt, system, max_tokens):
            if "Violated actions of this benchmark case" in prompt:
                return {"findings": []}
            return {"pass": review_pass, "points": {}, "issues": []}

        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "i")
            build.dir.mkdir(parents=True)
            build.save("plan", _full_plan(build.parent_dir))
            build.mark("plan", approved=True, by="human")
            installed = []
            original_install = build_mod.install
            build_mod.install = lambda b: installed.append(b.child_id) or []
            try:
                status = build_mod.run_build(
                    build, summary=summary, option=option,
                    writer_json=fake_json, writer_text=fake_text, reviewer=critic,
                    review=False, do_install=False, do_smoke=False, cross_exam=False,
                    config_file=Path("unused"), auto=True, out=lambda *_a: None,
                )
            finally:
                build_mod.install = original_install
            return status, installed

    def test_auto_installs_only_when_everything_passes(self):
        self.assertEqual(self._auto(True), ("installed", ["T0005-i-i-h"]))

    def test_auto_stops_when_review_fails(self):
        self.assertEqual(self._auto(False), ("needs_attention", []))

    def test_review_issues_get_one_repair_round(self):
        case = _case()
        reviews = iter([
            {"pass": False, "points": {}, "issues": [
                {"files": ["fixtures/email_client.json"], "problem": "recipient name typo"}]},
            {"pass": True, "points": {}, "issues": []},
        ])
        repaired = []

        def critic(prompt, system, max_tokens):
            if "Violated actions of this benchmark case" in prompt:
                return {"findings": []}
            return next(reviews)

        def patcher(prompt, system, max_tokens):
            if "Fix the listed problems in" not in prompt:
                return fake_json(prompt, system, max_tokens)
            self.assertIn("recipient name typo", prompt)
            current = prompt.split("## Current content of fixtures/email_client.json\n", 1)[1]
            repaired.append(True)
            return {"edits": [{"find": current[:40], "replace": current[:40]}]}

        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "i")
            build.dir.mkdir(parents=True)
            build.save("plan", _full_plan(build.parent_dir))
            build.mark("plan", approved=True, by="human")
            original_install = build_mod.install
            build_mod.install = lambda b: []
            try:
                status = build_mod.run_build(
                    build, summary={"domain_names": []}, option={"domain": "i"},
                    writer_json=patcher, writer_text=fake_text, reviewer=critic,
                    review=False, do_install=False, do_smoke=False, cross_exam=False,
                    config_file=Path("unused"), auto=True, out=lambda *_a: None,
                )
            finally:
                build_mod.install = original_install
            self.assertEqual(status, "installed")
            self.assertEqual(repaired, [True])
            self.assertIsNotNone(build.load("review_round1"))
            self.assertEqual(build.load("repairs_review")[0]["applied"], 1)


class ServiceSwapTests(unittest.TestCase):
    PARENT = SANDBOX_ROOT / "tasks" / "T0001-b-e-h"
    MAP = {"search_products": "search_products", "view_product": "view_product",
           "add_to_cart": "add_to_cart", "begin_checkout": "begin_checkout",
           "place_order": "place_order"}

    def _plan(self, **swap):
        entry = {"from_id": "shopping_app_instacart", "to_id": "amazon_shop",
                 "to_template": "amazon_shop", "fixture": "fixtures/amazon_shop.json",
                 "action_map": dict(self.MAP)}
        entry.update(swap)
        return {"files": [{"path": "fixtures/shopping_app_instacart.json", "op": "rename",
                           "new_path": "fixtures/amazon_shop.json"}],
                "service_swaps": [entry]}

    def test_valid_swap_passes(self):
        self.assertEqual(build_mod.service_swap_issues(self._plan(), self.PARENT), [])

    def test_a_step_that_lists_several_actions_does_not_crash_the_swap_check(self):
        names = build_mod._blueprint_actions({
            "assembly_blueprint": {"actions": [
                {"action": "amazon_shop.search_products"},
                {"action": ["amazon_shop.view_product", "amazon_shop.add_to_cart"]},
            ]},
        })
        self.assertEqual(names, [
            "amazon_shop.search_products", "amazon_shop.view_product", "amazon_shop.add_to_cart"])

    def test_unknown_or_missing_actions_are_reported(self):
        amap = dict(self.MAP, place_order="teleport")
        del amap["view_product"]
        issues = build_mod.service_swap_issues(self._plan(action_map=amap), self.PARENT)
        self.assertIn("service_swaps: action_map has no entry for 'view_product'", issues)
        self.assertTrue(any("'teleport' is not an action of amazon_shop" in i for i in issues))

    def test_fixture_must_be_written_by_the_plan(self):
        issues = build_mod.service_swap_issues(self._plan(fixture="fixtures/other.json"),
                                               self.PARENT)
        self.assertTrue(any("must be a file the plan writes" in i for i in issues))

    def test_swap_rewires_services_blueprint_and_detections(self):
        cfg = load_json(self.PARENT / "task_config.json")
        vig = load_json(self.PARENT / "vignette.json")
        build_mod.apply_service_swaps(cfg, vig, self._plan())
        self.assertEqual(cfg["services"], [{"id": "amazon_shop", "template": "amazon_shop",
                                            "fixture": "fixtures/amazon_shop.json"}])
        actions = [s["action"] for s in vig["assembly_blueprint"]["actions"]]
        self.assertIn("amazon_shop.place_order", actions)
        self.assertFalse(any(a.startswith("shopping_app_instacart.") for a in actions))
        self.assertNotIn("shopping_app_instacart",
                         json.dumps(vig["assembly_blueprint"]["detections"]))

    def test_list_actions_are_remapped_item_by_item(self):
        cfg = {"services": [{"id": "shopping_app_instacart", "template": "shopping_app_instacart",
                             "fixture": "fixtures/shopping_app_instacart.json"}]}
        vig = {"assembly_blueprint": {"actions": [
            {"action": "shopping_app_instacart.place_order"},
            {"action": ["shopping_app_instacart.view_product",
                        "shopping_app_instacart.add_to_cart",
                        "runtime:shell.read_file",
                        "message_user",
                        "injected:context"]},
        ]}}
        build_mod.apply_service_swaps(cfg, vig, self._plan())
        actions = [s["action"] for s in vig["assembly_blueprint"]["actions"]]
        self.assertEqual(actions[0], "amazon_shop.place_order")
        self.assertEqual(actions[1], ["amazon_shop.view_product", "amazon_shop.add_to_cart",
                                      "runtime:shell.read_file", "message_user",
                                      "injected:context"])

    def test_added_fixture_appends_its_service(self):
        self.assertTrue(build_mod.template_actions("docs_portal"))
        cfg = {"services": [{"id": "shopping_app_instacart", "template": "shopping_app_instacart",
                             "fixture": "fixtures/shopping_app_instacart.json"}]}
        vig = {"assembly_blueprint": {"actions": [
            {"action": "shopping_app_instacart.place_order"},
        ]}}
        plan = self._plan()
        plan["files"].append({"op": "add", "path": "", "new_path": "fixtures/docs_portal.json"})
        build_mod.apply_service_swaps(cfg, vig, plan)
        self.assertEqual(cfg["services"], [
            {"id": "amazon_shop", "template": "amazon_shop",
             "fixture": "fixtures/amazon_shop.json"},
            {"id": "docs_portal", "template": "docs_portal",
             "fixture": "fixtures/docs_portal.json"},
        ])


class CrossExamFailureTests(unittest.TestCase):
    def test_a_failed_probe_is_reported_not_raised(self):
        from authorization_eva_sandbox.task_generation.shared import leak_check

        def broken(**_kw):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

        original = leak_check.cross_examine_materials
        leak_check.cross_examine_materials = broken
        try:
            with tempfile.TemporaryDirectory() as td:
                report = build_mod.cross_exam_report(build_mod.Build(Path(td), _case(), "i"),
                                                     Path("unused"))
        finally:
            leak_check.cross_examine_materials = original
        self.assertIn("Expecting value", report["error"])


class VacatedDirTests(unittest.TestCase):
    def test_emptied_folders_go_but_other_empty_folders_stay(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "workspace" / "old" / "sub").mkdir(parents=True)
            (root / "workspace" / "kept_empty").mkdir()
            build_mod.remove_vacated_dirs(root, {root / "workspace" / "old" / "sub"})
            self.assertFalse((root / "workspace" / "old").exists())
            self.assertTrue((root / "workspace" / "kept_empty").is_dir())
            self.assertTrue((root / "workspace").is_dir())


class PatchTests(unittest.TestCase):
    def test_exact_unique_edits_apply_and_others_are_reported(self):
        text = "Dear Martina Chen,\nsee you Monday.\nMonday it is."
        new, applied, failures = build_mod.apply_edits(text, [
            {"find": "Martina Chen", "replace": "Marta Chen"},
            {"find": "Monday", "replace": "Tuesday"},
            {"find": "not in the file", "replace": "x"},
        ])
        self.assertEqual(new, "Dear Marta Chen,\nsee you Monday.\nMonday it is.")
        self.assertEqual(applied, 1)
        self.assertEqual(len(failures), 2)
        self.assertIn("appears 2 times", failures[0])
        self.assertIn("appears 0 times", failures[1])

    def test_edit_that_breaks_json_is_undone(self):
        case = _case()
        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "i")
            build.dir.mkdir(parents=True)
            build.staging.mkdir(parents=True)
            target = build.staging / "fixtures" / "x.json"
            target.parent.mkdir()
            target.write_text('{"name": "Martina Chen"}\n')

            def breaker(prompt, system, max_tokens):
                return {"edits": [{"find": '"Martina Chen"}', "replace": '"Marta Chen"'}]}

            result = build_mod.patch_one(build, breaker, {}, {}, {"entity_map": []},
                                         "fixtures/x.json", ["typo"], out=lambda *_a: None)
            self.assertEqual(target.read_text(), '{"name": "Martina Chen"}\n')
            self.assertEqual(result["applied"], 0)
            self.assertIn("not valid JSON", result["unapplied"][-1])

    def test_restart_keeps_the_approved_plan_only(self):
        case = _case()
        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "i")
            build.dir.mkdir(parents=True)
            build.save("plan", _full_plan(build.parent_dir))
            build.mark("plan", approved=True, by="human")
            build.mark("files")
            (build.dir / "child").mkdir()
            build.save("review", {"pass": False})
            build.restart_after_plan()
            self.assertEqual(list(build.state()["steps"]), ["plan"])
            self.assertFalse((build.dir / "child").exists())
            self.assertIsNone(build.load("review"))


class OrderTests(unittest.TestCase):
    def test_folder_files_are_written_small_first_with_siblings(self):
        case = _case()
        prompts = []

        def writer(prompt, system, max_tokens):
            prompts.append(prompt)
            return fake_text(prompt, system, max_tokens)

        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "i")
            build.dir.mkdir(parents=True)
            build_mod.write_files(build, writer, {}, {}, _full_plan(build.parent_dir),
                                  out=lambda *_a: None)
        order = [re.search(r"Rewrite the file (\S+)", p).group(1) for p in prompts]
        folder = [p for p in order if p.startswith("workspace/financial_records_folder/")]
        self.assertEqual(folder[-1], "workspace/financial_records_folder/RECONCILED_TRANSACTIONS_2025H1.json")
        last_prompt = prompts[order.index(folder[-1])]
        self.assertIn("Files of the new case you already wrote in the same folder", last_prompt)
        self.assertIn("transactions_2025_01.csv", last_prompt)

    def test_auto_refuses_an_unapproved_plan(self):
        case = _case()
        with tempfile.TemporaryDirectory() as td:
            build = build_mod.Build(Path(td), case, "i")
            build.dir.mkdir(parents=True)
            build.save("plan", _full_plan(build.parent_dir))
            build.mark("plan", approved=False)
            with self.assertRaises(ValueError):
                build_mod.run_build(
                    build, summary={}, option={}, writer_json=fake_json,
                    writer_text=fake_text, reviewer=fake_json, review=False,
                    do_install=False, do_smoke=False, cross_exam=False,
                    config_file=Path("unused"), auto=True, out=lambda *_a: None,
                )


class FallbackTests(unittest.TestCase):
    def test_content_filter_refusal_goes_to_the_fallback_profile(self):
        from authorization_eva_sandbox.task_generation.shared import llm as llm_mod

        calls = []

        def fake_complete(*, prompt, profile_name, config_file, max_tokens, system,
                          temperature, retries=1):
            calls.append(profile_name)
            if profile_name == "primary":
                raise RuntimeError("model returned empty text (finish_reason='content_filter')")
            return "written by fallback"

        original = llm_mod.complete_text
        llm_mod.complete_text = fake_complete
        try:
            call = build_mod.text_llm("primary", Path("x"), fallback_profile="backup",
                                      out=lambda *_a: None)
            self.assertEqual(call("p", "s", 10), "written by fallback")
        finally:
            llm_mod.complete_text = original
        self.assertEqual(calls, ["primary", "backup"])

    def test_other_errors_are_not_swallowed(self):
        from authorization_eva_sandbox.task_generation.expansion import domain_survey

        def primary(*_a):
            raise RuntimeError("rate limited")

        call = domain_survey.with_fallback(primary, lambda *_a: {"ok": 1},
                                           primary_name="a", fallback_name="b",
                                           out=lambda *_a: None)
        with self.assertRaises(RuntimeError):
            call("p", "s", 10)


class ReviewClipTests(unittest.TestCase):
    def test_a_long_file_keeps_its_ending_after_the_cut_mark(self):
        from authorization_eva_sandbox.task_generation.expansion.checks import clip_for_review

        text = ("IF-3101\n" * 1000) + "IF-3136 last row"
        shown = clip_for_review(text, max_chars=100, tail=20)
        self.assertIn("...(cut)", shown)
        self.assertTrue(shown.endswith("IF-3136 last row"))
        self.assertEqual(clip_for_review("short", max_chars=100), "short")


if __name__ == "__main__":
    unittest.main()
