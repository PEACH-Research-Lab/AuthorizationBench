from __future__ import annotations

import copy
import json
import unittest

from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    build_fill_envelope,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    compile_world_realization,
    prepare_world_realization,
    realization_sha256,
    schema_mismatch_findings,
    validate_world_realization,
)
from authorization_eva_sandbox.task_generation.shared.world_time_repair import (
    _parse_timeplan_reply,
    repair_timeplan_findings,
)
from authorization_eva_sandbox.task_generation.shared.world_repair import (
    PATCHABLE_OWNER,
    apply_world_patches,
    build_value_repair_prompt,
    group_repairable_findings,
    parse_replacement_text,
    repair_format_mismatches,
    repair_from_review,
    transactional_world_repair,
    world_dependency_closure,
)
from authorization_eva_sandbox.tests.test_world_fill_envelope import _load
from authorization_eva_sandbox.tests.test_world_realization_contract import (
    joint_graph_to_realization,
)


def _replacement(text: str) -> str:
    return f"REPLACEMENT\n{text}\nEND REPLACEMENT"


class WorldRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()
        cls.envelope = build_fill_envelope(
            scenario_id="V0003-g-b-h",
            policy_skeleton=_load("V0003-g-b-h", "policy_skeleton"),
            script=_load("V0003-g-b-h", "script"),
            blueprint=_load("V0003-g-b-h", "blueprint"),
            registry=cls.registry,
        )
        cls.realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), cls.envelope
        )

    def _body_target(self, index: int = 0) -> dict:
        record = self.realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][index]
        return {
            "target_id": "service:o1:email_client",
            "collection_path": "emails",
            "record_key": record["record_key"],
            "field_path": "emails[].body",
            "current_value": record["values"]["emails[].body"],
        }

    def _review_finding(
        self,
        target: dict,
        *,
        problem: str,
        suggestion: str,
        condition_index: int = 0,
        repairable: bool = True,
        kind: str = "field",
        address: dict | None = None,
    ) -> dict:
        resolved = address or {
            key: value for key, value in target.items() if key != "current_value"
        }
        display = " > ".join(
            str(resolved[key])
            for key in (
                "target_id", "collection_path", "record_key", "field_path",
            )
            if resolved.get(key)
        )
        return {
            "where": f"m2[{condition_index}] @ {display}",
            "problem": problem,
            "suggestion": suggestion,
            "material_id": "m2",
            "condition_index": condition_index,
            "location": {
                "display": display,
                "kind": kind,
                "address": resolved,
                "repairable": repairable,
            },
        }

    def test_base_hash_mismatch_is_rejected(self):
        target = self._body_target()
        allowed = [target]
        patched, issues = apply_world_patches(
            self.realization,
            {
                "base_sha256": "0" * 64,
                "patches": [{
                    "op": "replace",
                    "target": target,
                    "current_value": target["current_value"],
                    "value": "updated",
                }],
            },
            allowed_targets=allowed,
        )
        self.assertTrue(issues)
        self.assertEqual(patched, self.realization)

    def test_current_value_mismatch_is_rejected(self):
        target = self._body_target()
        _, issues = apply_world_patches(
            self.realization,
            {
                "base_sha256": realization_sha256(self.realization),
                "patches": [{
                    "op": "replace",
                    "target": target,
                    "current_value": "stale",
                    "value": "updated",
                }],
            },
            allowed_targets=[target],
        )
        self.assertTrue(any("current_value" in issue for issue in issues))

    def test_patch_outside_closure_is_rejected(self):
        target = self._body_target(0)
        other = self._body_target(1)
        _, issues = apply_world_patches(
            self.realization,
            {
                "base_sha256": realization_sha256(self.realization),
                "patches": [{
                    "op": "replace",
                    "target": other,
                    "current_value": other["current_value"],
                    "value": "updated",
                }],
            },
            allowed_targets=[target],
        )
        self.assertTrue(any("immutable" in issue for issue in issues))

    def test_unrelated_values_remain_byte_identical(self):
        target = self._body_target(0)
        original = copy.deepcopy(self.realization)
        replacement = "Claim approved; payment pending. Case R-4419."

        def _call(_prompt: str) -> str:
            return _replacement(replacement)

        patched, audit = repair_from_review(
            self.realization,
            findings=[self._review_finding(
                target,
                problem="body is incomplete",
                suggestion="include the case reference",
            )],
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_call,
        )
        self.assertTrue(audit["applied"])
        original_record = original["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        patched_record = patched["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        self.assertEqual(patched_record["values"]["emails[].body"], replacement)
        original_record["values"]["emails[].body"] = replacement
        self.assertEqual(
            json.dumps(original, sort_keys=True),
            json.dumps(patched, sort_keys=True),
        )

    def test_structural_failure_rolls_back(self):
        target = self._body_target(0)
        allowed = world_dependency_closure(self.realization, [{
            "owner": PATCHABLE_OWNER,
            "targets": [target],
            "kind": "condition_missing",
        }])
        result, receipt = transactional_world_repair(
            self.realization,
            {
                "base_sha256": realization_sha256(self.realization),
                "patches": [{
                    "op": "replace",
                    "target": target,
                    "current_value": target["current_value"],
                    "value": 12,
                }],
            },
            findings=[],
            allowed_targets=allowed,
            envelope=self.envelope,
            registry=self.registry,
        )
        self.assertTrue(receipt["rolled_back"])
        self.assertFalse(receipt["applied"])
        self.assertEqual(result, self.realization)

    def test_malformed_replacement_does_not_change_the_world(self):
        target = self._body_target(0)
        calls = {"n": 0}

        def _call(_prompt: str) -> str:
            calls["n"] += 1
            return "sure, I'll patch it"

        result, audit = repair_from_review(
            self.realization,
            findings=[self._review_finding(
                target,
                problem="body is incomplete",
                suggestion="include the case reference",
            )],
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_call,
        )
        self.assertEqual(calls["n"], 1)
        self.assertTrue(audit["stopped"])
        self.assertTrue(audit["rolled_back"])
        self.assertFalse(audit["applied"])
        self.assertEqual(result, self.realization)

    def test_two_findings_on_one_value_make_one_replacement(self):
        target = self._body_target(0)
        prompts: list[str] = []
        replacement = target["current_value"] + " Case R-4419. Payment pending."

        def _call(prompt: str) -> str:
            prompts.append(prompt)
            self.assertIn("body is incomplete", prompt)
            self.assertIn("payment remains pending", prompt)
            self.assertNotIn('"patches"', prompt)
            self.assertNotIn("base_sha256", prompt)
            return _replacement(replacement)

        result, audit = repair_from_review(
            self.realization,
            findings=[
                self._review_finding(
                    target,
                    problem="body is incomplete",
                    suggestion="include the case reference",
                    condition_index=4,
                ),
                self._review_finding(
                    target,
                    problem="payment remains pending is missing",
                    suggestion="state that payment is still pending",
                    condition_index=4,
                ),
            ],
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_call,
        )
        self.assertEqual(len(prompts), 1)
        self.assertTrue(audit["applied"])
        self.assertEqual(audit["replacements"][0]["finding_count"], 2)
        request = audit["requests"][0]
        self.assertEqual(request["current_value"], target["current_value"])
        self.assertEqual(len(request["findings"]), 2)
        self.assertIn("body is incomplete", request["prompt"])
        self.assertEqual(request["raw_reply"], _replacement(replacement))
        self.assertEqual(request["replacement"], replacement)
        self.assertEqual(
            result["targets"]["service:o1:email_client"]["collections"][
                "emails"
            ]["records"][0]["values"]["emails[].body"],
            replacement,
        )

    def test_non_repairable_finding_stops_without_partial_repair(self):
        target = self._body_target(0)
        other = self._body_target(1)
        calls = {"n": 0}

        def _call(_prompt: str) -> str:
            calls["n"] += 1
            raise AssertionError("repair must not run")

        collection = self._review_finding(
            target,
            problem="listing is not paginated",
            suggestion="partition across pages",
            kind="collection",
            repairable=False,
            address={
                "target_id": "service:o1:email_client",
                "collection_path": "emails",
            },
        )
        result, audit = repair_from_review(
            self.realization,
            findings=[
                collection,
                self._review_finding(
                    other,
                    problem="body is incomplete",
                    suggestion="include the case reference",
                ),
            ],
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_call,
        )
        self.assertEqual(calls["n"], 0)
        self.assertTrue(audit["stopped"])
        self.assertFalse(audit["attempted"])
        self.assertEqual(result, self.realization)
        self.assertTrue(any("non-repairable" in item for item in audit["problems"]))

    def test_successful_repair_does_not_regenerate_world(self):
        target = self._body_target(0)
        replacement = target["current_value"] + " Case R-4419."
        result, audit = repair_from_review(
            self.realization,
            findings=[self._review_finding(
                target,
                problem="body is incomplete",
                suggestion="include the case reference",
            )],
            envelope=self.envelope,
            registry=self.registry,
            repair_call=lambda _prompt: _replacement(replacement),
        )
        self.assertTrue(audit["applied"])
        self.assertFalse(audit["rolled_back"])
        self.assertEqual(
            result["targets"]["service:o1:email_client"]["collections"][
                "emails"
            ]["count"],
            self.realization["targets"]["service:o1:email_client"][
                "collections"
            ]["emails"]["count"],
        )
        self.assertEqual(
            len(result["targets"]["service:o1:email_client"]["collections"][
                "emails"
            ]["records"]),
            30,
        )

    def test_invalid_address_does_not_broaden_scope(self):
        target = self._body_target(0)
        finding = self._review_finding(
            target,
            problem="body is incomplete",
            suggestion="include the case reference",
            address={
                "target_id": "service:o1:email_client",
                "collection_path": "emails",
                "record_key": "missing-record",
                "field_path": "emails[].body",
            },
        )
        groups, problems = group_repairable_findings(self.realization, [finding])
        self.assertEqual(groups, [])
        self.assertTrue(problems)
        result, audit = repair_from_review(
            self.realization,
            findings=[finding],
            envelope=self.envelope,
            registry=self.registry,
            repair_call=lambda _prompt: (_ for _ in ()).throw(
                AssertionError("repair must not run")
            ),
        )
        self.assertTrue(audit["stopped"])
        self.assertFalse(audit["attempted"])
        self.assertEqual(result, self.realization)

    def test_replacement_parser_rejects_fences_and_prose(self):
        value, problems = parse_replacement_text(
            _replacement("hello"), current_value="old"
        )
        self.assertEqual(value, "hello")
        self.assertEqual(problems, [])
        _, problems = parse_replacement_text(
            "```\nREPLACEMENT\nhello\nEND REPLACEMENT\n```",
            current_value="old",
        )
        self.assertTrue(any("markdown" in item for item in problems))
        _, problems = parse_replacement_text(
            "REPLACEMENT_JSON\n12\nEND REPLACEMENT_JSON",
            current_value="old",
        )
        self.assertTrue(any("REPLACEMENT block" in item for item in problems))
        value, problems = parse_replacement_text(
            "REPLACEMENT_JSON\n{\"ok\": true}\nEND REPLACEMENT_JSON",
            current_value={"ok": False},
        )
        self.assertEqual(value, {"ok": True})
        self.assertEqual(problems, [])
        value, problems = parse_replacement_text(
            "REPLACEMENT_JSON\nfalse\nEND REPLACEMENT_JSON",
            current_value="false",
            schema="bool",
        )
        self.assertIs(value, False)
        self.assertEqual(problems, [])

    def test_repair_prompt_includes_field_contract_not_patch_json(self):
        target = self._body_target(0)
        groups, problems = group_repairable_findings(
            self.realization,
            [self._review_finding(
                target,
                problem="body is incomplete",
                suggestion="include the case reference",
                condition_index=4,
            )],
        )
        self.assertEqual(problems, [])
        prompt = build_value_repair_prompt(
            realization=self.realization,
            group=groups[0],
            envelope=self.envelope,
            registry=self.registry,
        )
        self.assertIn("emails[].body", prompt)
        self.assertIn('"source": "model"', prompt)
        self.assertIn("plain_text", prompt)
        self.assertIn("body is incomplete", prompt)
        self.assertIn("Do not emit an address", prompt)
        self.assertIn("field contract schema", prompt)
        self.assertNotIn('"patches"', prompt)
        self.assertNotIn("base_sha256", prompt)

    def _email_record(self, realization: dict, index: int = 0) -> dict:
        return realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][index]

    def test_format_repair_replaces_enum_mismatch(self):
        realization = copy.deepcopy(self.realization)
        record = self._email_record(realization)
        record["values"]["emails[].folder"] = "Inbox"
        prepare_world_realization(
            realization, self.envelope, registry=self.registry
        )
        findings = schema_mismatch_findings(
            realization, self.envelope, self.registry
        )
        self.assertTrue(
            any(
                item["location"]["address"].get("field_path") == "emails[].folder"
                for item in findings
            )
        )
        before = validate_world_realization(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            prepare=False,
        )
        self.assertTrue(any("must be one of" in issue for issue in before))
        calls: list[str] = []

        def _call(prompt: str) -> str:
            calls.append(prompt)
            return _replacement("inbox")

        patched, audit = repair_format_mismatches(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_call,
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("enum:inbox|sent|drafts|archive|trash", calls[0])
        self.assertNotIn("FINDING", calls[0])
        self.assertTrue(audit["applied"])
        self.assertEqual(
            self._email_record(patched)["values"]["emails[].folder"],
            "inbox",
        )
        self.assertEqual(
            validate_world_realization(
                patched,
                envelope=self.envelope,
                registry=self.registry,
                prepare=False,
            ),
            [],
        )

    def test_format_repair_replaces_bool_type_mismatch(self):
        realization = copy.deepcopy(self.realization)
        record = self._email_record(realization)
        record["values"]["emails[].read"] = "false"
        prepare_world_realization(
            realization, self.envelope, registry=self.registry
        )
        calls: list[str] = []

        def _call(prompt: str) -> str:
            calls.append(prompt)
            return (
                "REPLACEMENT_JSON\nfalse\nEND REPLACEMENT_JSON"
            )

        patched, audit = repair_format_mismatches(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_call,
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(audit["applied"])
        self.assertIs(self._email_record(patched)["values"]["emails[].read"], False)
        self.assertEqual(
            validate_world_realization(
                patched,
                envelope=self.envelope,
                registry=self.registry,
                prepare=False,
            ),
            [],
        )

    def test_list_wrap_does_not_invoke_format_repair(self):
        realization = copy.deepcopy(self.realization)
        record = self._email_record(realization)
        realization["shared_values"] = dict(realization.get("shared_values") or {})
        realization["shared_values"]["ops_list_address"] = (
            "devops-list@lists.example.com"
        )
        record["values"]["emails[].to"] = {"$ref": "ops_list_address"}
        prepare_world_realization(
            realization, self.envelope, registry=self.registry
        )
        self.assertEqual(
            record["values"]["emails[].to"],
            ["devops-list@lists.example.com"],
        )

        def _boom(_prompt: str) -> str:
            raise AssertionError("format repair must not run after code wrap")

        _patched, audit = repair_format_mismatches(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            repair_call=_boom,
        )
        self.assertFalse(audit["attempted"])
        self.assertEqual(audit["findings"], [])

    def test_missing_required_is_not_a_format_finding(self):
        realization = copy.deepcopy(self.realization)
        prepare_world_realization(
            realization, self.envelope, registry=self.registry
        )
        record = self._email_record(realization)
        record["values"].pop("emails[].body", None)
        findings = schema_mismatch_findings(
            realization, self.envelope, self.registry
        )
        self.assertFalse(
            any(
                item["location"]["address"].get("field_path") == "emails[].body"
                for item in findings
            )
        )
        issues = validate_world_realization(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            prepare=False,
        )
        self.assertTrue(
            any("missing required model fields" in issue for issue in issues)
        )

    def test_collection_time_finding_localizes_to_sparse_anchors(self):
        realization = copy.deepcopy(self.realization)
        prepare_world_realization(
            realization, self.envelope, registry=self.registry
        )
        fixtures, _runtime, _events, _reports, issues = compile_world_realization(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            principal={"user_email": "priya@example.com"},
        )
        self.assertEqual(issues, [])
        target_id = "service:o1:email_client"
        records = realization["targets"][target_id]["collections"]["emails"][
            "records"
        ]
        first_key = records[1]["record_key"]
        later_key = records[19]["record_key"]
        finding = {
            "where": f"m2[6] @ {target_id} > emails",
            "problem": "The old thread and recent reply have nearly equal dates.",
            "suggestion": "Make the parent old and its reply recent.",
            "material_id": "m2",
            "condition_index": 6,
            "location": {
                "display": f"{target_id} > emails",
                "kind": "collection",
                "address": {
                    "target_id": target_id,
                    "collection_path": "emails",
                },
                "repairable": False,
            },
        }

        def _call(prompt: str) -> str:
            first = (
                f"{target_id} > emails > {first_key} > emails[].timestamp"
            )
            later = (
                f"{target_id} > emails > {later_key} > emails[].timestamp"
            )
            self.assertIn(first, prompt)
            self.assertIn(later, prompt)
            return (
                "TIMEPLAN_REPAIR\n"
                f"WHERE: {first}\n"
                "OFFSET: -125d\n"
                "END\n"
                "TIMEPLAN_REPAIR\n"
                f"WHERE: {later}\n"
                "OFFSET: -41m\n"
                "END"
            )

        patched, remaining, audit = repair_timeplan_findings(
            realization,
            findings=[finding],
            envelope=self.envelope,
            registry=self.registry,
            fixtures=fixtures,
            reference_time="2026-06-06T10:11:00Z",
            repair_call=_call,
        )
        self.assertEqual(remaining, [])
        self.assertTrue(audit["applied"])
        self.assertEqual(len(patched["timeplan_overrides"]), 2)
        repaired, _runtime, _events, reports, issues = compile_world_realization(
            patched,
            envelope=self.envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            principal={"user_email": "priya@example.com"},
        )
        self.assertEqual(issues, [])
        emails = repaired["email_client"]["emails"]
        timestamps = [item["timestamp"] for item in emails]
        self.assertEqual(timestamps[1], "2026-02-01T10:11:00Z")
        self.assertEqual(timestamps[19], "2026-06-06T09:30:00Z")
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(
            all(item["created_at"] == item["timestamp"] for item in emails)
        )
        self.assertTrue(
            all(item["updated_at"] == item["timestamp"] for item in emails)
        )
        changes = reports[target_id]["timeplan_override_changes"]
        self.assertTrue(any(item["record_key"] == first_key for item in changes))
        self.assertTrue(audit["requests"][0]["finding"]["problem"])

    def test_non_temporal_collection_finding_stays_non_repairable(self):
        realization = copy.deepcopy(self.realization)
        prepare_world_realization(
            realization, self.envelope, registry=self.registry
        )
        fixtures, _runtime, _events, _reports, issues = compile_world_realization(
            realization,
            envelope=self.envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
        )
        self.assertEqual(issues, [])
        finding = {
            "problem": "Listing is not paginated.",
            "suggestion": "Use pagination.",
            "material_id": "m2",
            "condition_index": 1,
            "location": {
                "display": "service:o1:email_client > emails",
                "kind": "collection",
                "address": {
                    "target_id": "service:o1:email_client",
                    "collection_path": "emails",
                },
                "repairable": False,
            },
        }
        patched, remaining, audit = repair_timeplan_findings(
            realization,
            findings=[finding],
            envelope=self.envelope,
            registry=self.registry,
            fixtures=fixtures,
            reference_time="2026-06-06T10:11:00Z",
            repair_call=lambda _prompt: "NOT_TIMEPLAN",
        )
        self.assertEqual(patched, realization)
        self.assertEqual(remaining, [finding])
        self.assertFalse(audit["applied"])

    def test_timeplan_offset_is_bounded_by_reference_time(self):
        display = "service:o1:email_client > emails > e01 > emails[].timestamp"
        status, replacements, problems = _parse_timeplan_reply(
            (
                "TIMEPLAN_REPAIR\n"
                f"WHERE: {display}\n"
                "OFFSET: +1000d\n"
                "END"
            ),
            [{
                "display": display,
                "address": {
                    "target_id": "service:o1:email_client",
                    "collection_path": "emails",
                    "record_key": "e01",
                    "field_path": "emails[].timestamp",
                },
            }],
            reference_time="2026-06-06T10:11:00Z",
        )
        self.assertEqual(status, "error")
        self.assertEqual(replacements, [])
        self.assertTrue(any("NOW-5y..NOW+45d" in item for item in problems))

    def test_compile_code_fixes_then_one_fill_covers_required_holes(self):
        from authorization_eva_sandbox.task_generation.shared.world_repair import (
            apply_compile_code_fixes,
            build_fill_gaps_prompt,
            missing_required_holes,
            parse_and_apply_fill_reply,
        )
        from authorization_eva_sandbox.tests.test_world_realization_contract import (
            _envelope,
        )
        registry = ServiceRegistry.load()
        envelope = _envelope(registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        records = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"]
        records[0]["values"]["emails[].from"] = {"$ref": "missing.person"}
        records[0]["values"].pop("emails[].body", None)
        records.append(copy.deepcopy(records[0]))
        records.append(copy.deepcopy(records[0]))
        records[-1]["values"]["emails[].body"] = "a different body"
        audit = apply_compile_code_fixes(realization, envelope, registry)
        actions = {item["action"] for item in audit["actions"]}
        self.assertIn("drop_duplicate_record", actions)
        self.assertIn("rename_duplicate_record", actions)
        self.assertIn("drop_bad_field", actions)
        holes = missing_required_holes(realization, envelope, registry)
        self.assertTrue(any(hole["address"]["field_path"] == "emails[].body" for hole in holes))
        self.assertTrue(any(hole["address"]["field_path"] == "emails[].from" for hole in holes))
        prompt = build_fill_gaps_prompt(holes, reference_time="2026-06-06T10:11:00")
        self.assertIn("Fill every missing field", prompt)
        self.assertIn("2026-06-06T10:11:00", prompt)
        reply = []
        for hole in holes:
            if hole["address"]["field_path"] == "emails[].from":
                reply.append(
                    f"WHERE: {hole['where']}\nREPLACEMENT\npriya@example.com\nEND REPLACEMENT"
                )
            elif hole["address"]["field_path"] == "emails[].body":
                reply.append(
                    f"WHERE: {hole['where']}\nREPLACEMENT\nfilled body\nEND REPLACEMENT"
                )
        parse_and_apply_fill_reply(realization, holes, "\n".join(reply))
        filled = records[0]["values"]
        self.assertEqual(filled["emails[].from"], "priya@example.com")
        self.assertEqual(filled["emails[].body"], "filled body")


if __name__ == "__main__":
    unittest.main()
