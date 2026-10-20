from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    build_fill_envelope,
)
from authorization_eva_sandbox.services.fixture_values import (
    compile_target_fixture,
    resolve_shared_values,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    SCHEMA_VERSION,
    WorldRealizationError,
    bind_authored_name,
    build_world_realization_continuation_prompt,
    build_world_realization_prompt,
    canonicalize_world_realization,
    classify_world_generation_failure,
    apply_timeplan_overrides,
    compile_world_realization,
    parse_world_realization,
    parse_world_realization_text,
    prepare_world_realization,
    realization_sha256,
    rewind_incomplete_world_json,
    stitch_world_realization_text,
    validate_world_realization,
    _record_timeplan_overrides,
)
from authorization_eva_sandbox.task_generation.shared.world_field_classify import (
    ClassificationItem,
    MenuOption,
    coerce_answer_text,
    parse_answer_key,
)
from authorization_eva_sandbox.tests.test_world_fill_envelope import _load_live


FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/v4"


def _load(stem: str, kind: str) -> dict:
    return json.loads((FIXTURES / f"{stem}.{kind}.json").read_text())


def _envelope(registry, stem: str):
    return build_fill_envelope(
        scenario_id=stem,
        policy_skeleton=_load(stem, "policy_skeleton"),
        script=_load(stem, "script"),
        blueprint=_load(stem, "blueprint"),
        registry=registry,
    )


def _classify_wanted(wanted: dict[str, str]):
    """Pick menu letters from authored labels onto closed paths."""

    def _call(prompt: str) -> dict[str, str]:
        answers: list[str] = []
        current = ""
        label = ""
        options: list[tuple[str, str]] = []

        def flush() -> None:
            if not current or not label:
                return
            path = wanted.get(label)
            if path is None:
                return
            for letter, option_path in options:
                if option_path == path:
                    answers.append(f"{current}{letter}")
                    return

        for line in prompt.splitlines():
            if line.startswith("#") and line[1:].strip().isdigit():
                flush()
                current = line[1:].strip()
                label = ""
                options = []
            elif line.startswith("label: "):
                label = line[len("label: "):]
            elif len(line) >= 4 and line[0].isalpha() and line[1:3] == "  ":
                letter = line[0]
                path = line[3:].split(" —", 1)[0].strip()
                options.append((letter, path))
        flush()
        return {"answers": "\n".join(answers)}

    return _call


def joint_graph_to_realization(graph: dict, envelope) -> dict:
    """Test-only conversion of cached joint graphs onto envelope targets."""
    materials = graph["graph"]["materials"]
    service: dict[str, dict] = {}
    runtime: dict[str, dict] = {}
    for material_id, material in envelope.materials.items():
        if material.coverage_without_model:
            continue
        payload = copy.deepcopy(materials[material_id])
        collections = {}
        for path, collection in (payload.get("collections") or {}).items():
            records = []
            for index, record in enumerate(collection.get("records") or []):
                records.append({
                    "record_key": f"{material_id}-{path}-{index}",
                    "values": copy.deepcopy(record.get("values") or {}),
                })
            collections[path] = {
                "mode": collection.get("mode") or "exhaustive",
                "count": collection.get("count", len(records)),
                "records": records,
            }
        target = envelope.targets[material.target_id]
        bucket = runtime if target.kind == "runtime" else service
        entry = bucket.setdefault(material.target_id, {
            "scalar_values": {},
            "collections": {},
        })
        entry["scalar_values"].update(payload.get("scalar_values") or {})
        entry["collections"].update(collections)
    return {
        "schema_version": SCHEMA_VERSION,
        "shared_values": {},
        "time_anchors": {},
        "targets": service,
        "runtime_targets": runtime,
        "environment_events": [],
    }


class WorldRealizationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def test_round_trip_and_stable_hash(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        parsed = parse_world_realization(json.loads(json.dumps(realization)))
        self.assertEqual(parsed, realization)
        self.assertEqual(
            realization_sha256(parsed), realization_sha256(realization)
        )

    def test_text_parser_rejects_nested_record_fragment(self):
        fragment = json.dumps({
            "record_key": "e06",
            "values": {"subject": "Order NL-44871"},
        })
        with self.assertRaises(WorldRealizationError) as raised:
            parse_world_realization_text(fragment)
        self.assertIn("missing top-level keys", str(raised.exception))

    def test_text_parser_requires_one_complete_document(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        fenced = "```json\n" + json.dumps(realization) + "\n```"
        self.assertEqual(parse_world_realization_text(fenced), realization)
        with self.assertRaises(WorldRealizationError):
            parse_world_realization_text(
                json.dumps(realization) + "\n" + json.dumps({"extra": True})
            )

    def test_generation_failure_kind_and_json_continuation(self):
        truncated = """{
  "schema_version": 2,
  "shared_values": {"k": "v"},
  "time_anchors": {},
  "targets": {
    "service:o2:email_client": {
      "scalar_values": {},
      "collections": {
        "emails": {
          "mode": "exhaustive",
          "count": 2,
          "records": [
            {
              "record_key": "keep_me",
              "values": {"subject": "ok"}
            },
            {
              "record_key": "cut_off",
              "values": {"body": "Prepared ADDITION"""
        with self.assertRaises(WorldRealizationError) as raised:
            parse_world_realization_text(truncated)
        self.assertEqual(
            classify_world_generation_failure(raised.exception, truncated),
            "incomplete_json",
        )
        self.assertEqual(
            classify_world_generation_failure(
                RuntimeError("model returned empty text (finish_reason='length')"),
                "",
            ),
            "empty",
        )
        extra = '{"schema_version": 2}\n{"extra": true}'
        with self.assertRaises(WorldRealizationError) as extra_exc:
            parse_world_realization_text(extra)
        self.assertEqual(
            classify_world_generation_failure(extra_exc.exception, extra),
            "replacement",
        )
        fragment = json.dumps({"record_key": "e06", "values": {"subject": "x"}})
        with self.assertRaises(WorldRealizationError) as shape_exc:
            parse_world_realization_text(fragment)
        self.assertEqual(
            classify_world_generation_failure(shape_exc.exception, fragment),
            "replacement",
        )
        prefix = rewind_incomplete_world_json(truncated)
        self.assertIsNotNone(prefix)
        self.assertIn('"keep_me"', prefix)
        self.assertNotIn("cut_off", prefix)
        self.assertFalse(prefix.endswith(","))
        continuation = """
          ]
        }
      }
    }
  },
  "runtime_targets": {},
  "environment_events": []
}
"""
        stitched = stitch_world_realization_text(prefix, continuation)
        parsed = parse_world_realization_text(stitched)
        self.assertEqual(
            parsed["targets"]["service:o2:email_client"]["collections"]
            ["emails"]["records"][0]["record_key"],
            "keep_me",
        )
        restarted = stitch_world_realization_text(
            prefix,
            '{"schema_version": 2, "shared_values": {}, "time_anchors": {}, '
            '"targets": {}, "runtime_targets": {}, "environment_events": []}',
        )
        self.assertTrue(restarted.lstrip().startswith("{"))
        self.assertNotIn("keep_me", restarted)
        prompt = build_world_realization_continuation_prompt(
            original_prompt="ORIGINAL",
            prefix=prefix,
            problems=["unterminated string"],
        )
        self.assertIn("ORIGINAL", prompt)
        self.assertIn(prefix, prompt)
        self.assertIn("Return ONLY the continuation text", prompt)
        self.assertNotIn("COMPLETE replacement JSON document", prompt)


    def test_record_relative_time_offsets_compile_against_now(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        records = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"]
        records[1]["time_offset"] = "-125d"
        records[19]["time_offset"] = "-41m"
        fixtures, _runtime, _events, reports, issues = compile_world_realization(
            realization,
            envelope=envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00Z",
            principal={"user_email": "priya@example.com"},
        )
        self.assertEqual(issues, [])
        emails = fixtures["email_client"]["emails"]
        self.assertEqual(emails[1]["timestamp"], "2026-02-01T10:11:00Z")
        self.assertEqual(emails[19]["timestamp"], "2026-06-06T09:30:00Z")
        self.assertTrue(
            reports["service:o1:email_client"]["timeplan_override_changes"]
        )

    def test_unknown_field_is_not_dropped(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        record["values"]["emails[].not_a_field"] = "secret"
        before = copy.deepcopy(record["values"])
        canonicalize_world_realization(realization)
        self.assertEqual(
            realization["targets"]["service:o1:email_client"][
                "collections"
            ]["emails"]["records"][0]["values"],
            before,
        )
        audit = prepare_world_realization(realization, envelope)
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        self.assertEqual(record["unbound_values"]["emails[].not_a_field"], "secret")
        self.assertNotIn("emails[].not_a_field", record["values"])
        self.assertTrue(any(item.get("from") == "emails[].not_a_field" for item in audit["unbound"]))
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertFalse(any("unknown fields" in issue for issue in issues))

    def test_extra_collections_are_dropped_not_failed(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        collections = realization["targets"]["service:o1:email_client"][
            "collections"
        ]
        collections["tree_specs"] = {
            "mode": "exhaustive",
            "count": 1,
            "records": [{"record_key": "extra_tree", "values": {}}],
        }
        collections["meeting.participants[].sessions"] = {
            "mode": "exhaustive",
            "count": 1,
            "records": [{"record_key": "extra_session", "values": {}}],
        }
        audit = prepare_world_realization(realization, envelope)
        remaining = realization["targets"]["service:o1:email_client"][
            "collections"
        ]
        self.assertIn("emails", remaining)
        self.assertNotIn("tree_specs", remaining)
        self.assertNotIn("meeting.participants[].sessions", remaining)
        self.assertTrue(
            any(
                item.get("from") == "tree_specs" and item.get("dropped")
                for item in audit["unbound"]
            )
        )
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertFalse(any("unknown collections" in issue for issue in issues))

    def test_time_offset_fills_start_time_and_drops_when_unusable(self):
        template = type("T", (), {})()
        template.fields = [
            type("F", (), {
                "path": "meeting.transcript.segments[].start_time",
                "source": "timeplan",
            })(),
            type("F", (), {
                "path": "meeting.transcript.segments[].text",
                "source": "model",
            })(),
            type("F", (), {
                "path": "meeting.participants[].display_name",
                "source": "model",
            })(),
        ]
        payload = {
            "scalar_values": {},
            "collections": {
                "meeting.transcript.segments": {
                    "mode": "exhaustive",
                    "count": 1,
                    "records": [{
                        "record_key": "t01",
                        "time_offset": "-11m",
                        "values": {
                            "meeting.transcript.segments[].text": "hi",
                        },
                    }],
                },
                "meeting.participants": {
                    "mode": "exhaustive",
                    "count": 1,
                    "records": [{
                        "record_key": "p1",
                        "time_offset": "+33m",
                        "values": {
                            "meeting.participants[].display_name": "Marcus",
                        },
                    }],
                },
            },
        }
        overrides, issues = _record_timeplan_overrides(
            target_id="service:o2:zoom",
            payload=payload,
            template=template,
            reference_time="2026-06-06T10:11:00",
        )
        self.assertEqual(issues, [])
        self.assertEqual(len(overrides), 1)
        self.assertEqual(
            overrides[0]["field_path"],
            "meeting.transcript.segments[].start_time",
        )
        self.assertEqual(overrides[0]["value"], "2026-06-06T10:00:00Z")
        self.assertNotIn(
            "time_offset",
            payload["collections"]["meeting.participants"]["records"][0],
        )

    def test_equal_timeplan_anchors_do_not_fail(self):
        template = type("Template", (), {})()
        template.fields = [
            type("Field", (), {
                "path": "emails[].timestamp",
                "source": "timeplan",
            })(),
        ]
        same = "2026-06-05T10:11:00Z"
        fixture = {"emails": [{"timestamp": "old"}, {"timestamp": "old"}]}
        payload = {"collections": {"emails": {"records": [
            {"record_key": "a"},
            {"record_key": "b"},
        ]}}}
        overrides = [
            {
                "target_id": "service:o1",
                "collection_path": "emails",
                "record_key": key,
                "field_path": "emails[].timestamp",
                "value": same,
            }
            for key in ("a", "b")
        ]
        issues, _changes = apply_timeplan_overrides(
            fixture,
            target_id="service:o1",
            payload=payload,
            template=template,
            overrides=overrides,
            reference_time="2026-06-06T10:11:00",
        )
        self.assertEqual(issues, [])
        self.assertEqual(fixture["emails"][0]["timestamp"], same)
        self.assertEqual(fixture["emails"][1]["timestamp"], same)
        earlier = "2026-06-04T10:11:00Z"
        overrides[1]["value"] = earlier
        issues, _changes = apply_timeplan_overrides(
            fixture,
            target_id="service:o1",
            payload=payload,
            template=template,
            overrides=overrides,
            reference_time="2026-06-06T10:11:00",
        )
        self.assertEqual(issues, [])
        self.assertEqual(fixture["emails"][0]["timestamp"], same)
        self.assertEqual(fixture["emails"][1]["timestamp"], earlier)

    def test_exact_target_set_and_record_key_uniqueness(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        realization["targets"]["service:extra:email_client"] = {
            "scalar_values": {},
            "collections": {},
        }
        audit = prepare_world_realization(realization, envelope)
        self.assertNotIn(
            "service:extra:email_client", realization["targets"]
        )
        self.assertTrue(
            any(
                item.get("from") == "service:extra:email_client"
                and item.get("dropped")
                for item in audit["unbound"]
            )
        )
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertFalse(any("service targets" in issue for issue in issues))
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        realization["targets"].pop("service:o1:email_client", None)
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertTrue(any("missing service targets" in issue for issue in issues))
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        records = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"]
        records[1]["record_key"] = records[0]["record_key"]
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry
        )
        self.assertTrue(any("record_key" in issue for issue in issues))

    def test_forbidden_and_shared_ref_type_checks(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        realization["targets"]["service:o1:email_client"]["collections"][
            "emails"
        ]["records"][0]["values"]["emails[].id"] = "authored-id"
        audit = prepare_world_realization(realization, envelope)
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        self.assertNotIn("emails[].id", record["values"])
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertFalse(any("forbidden" in issue for issue in issues))
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        realization["shared_values"] = {"person.priya.email": "priya@example.com"}
        realization["targets"]["service:o1:email_client"]["collections"][
            "emails"
        ]["records"][0]["values"]["emails[].from"] = {
            "$ref": "person.priya.email"
        }
        self.assertEqual(
            validate_world_realization(
                realization, envelope=envelope, registry=self.registry
            ),
            [],
        )
        realization["targets"]["service:o1:email_client"]["collections"][
            "emails"
        ]["records"][0]["values"]["emails[].from"] = {
            "$ref": "missing.fact"
        }
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry
        )
        self.assertTrue(any("unresolved $ref" in issue for issue in issues))

    def test_model_authored_coverage_is_rejected(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        self.assertEqual(
            validate_world_realization(
                realization, envelope=envelope, registry=self.registry
            ),
            [],
        )
        realization["coverage"] = []
        prepare_world_realization(realization, envelope)
        self.assertNotIn("coverage", realization)
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertFalse(any("coverage" in issue for issue in issues))

    def test_prompt_contains_envelope_and_do_not_rules(self):
        envelope = _envelope(self.registry, "V0010-l-b-h")
        prompt = build_world_realization_prompt(
            envelope=envelope,
            story=_load("V0010-l-b-h", "story"),
            script=_load("V0010-l-b-h", "script"),
            policy_skeleton=_load("V0010-l-b-h", "policy_skeleton"),
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            forbidden="Do not name the violated action.",
        )
        self.assertIn("DO NOT", prompt)
        self.assertIn("service:o1:zoom", prompt)
        self.assertIn("service:o2:email_client", prompt)
        self.assertIn('"schema_version": 2', prompt)
        self.assertNotIn('"coverage"', prompt)
        self.assertNotIn("Emit coverage", prompt)
        self.assertIn("Invent action, template, slot, field", prompt)
        self.assertIn(envelope.instruction, prompt)
        self.assertIn("$record_ref", prompt)
        self.assertIn("field label", prompt)
        self.assertIn("hints for the same closed field", prompt)
        live = build_fill_envelope(
            scenario_id="V0010-l-b-h",
            policy_skeleton=_load_live("V0010-l-b-h", "policy_skeleton"),
            script=_load_live("V0010-l-b-h", "script"),
            blueprint=_load_live("V0010-l-b-h", "blueprint"),
            registry=self.registry,
        )
        live_prompt = build_world_realization_prompt(
            envelope=live,
            story=_load_live("V0010-l-b-h", "story"),
            script=_load_live("V0010-l-b-h", "script"),
            policy_skeleton=_load_live("V0010-l-b-h", "policy_skeleton"),
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            forbidden="Do not name the violated action.",
        )
        self.assertIn("service:o2:zoom", live_prompt)
        self.assertIn("service:o5:email_client", live_prompt)
        self.assertIn("DO NOT", live_prompt)

    def test_s0010_compiles_one_coherent_world(self):
        envelope = _envelope(self.registry, "V0010-l-b-h")
        realization = joint_graph_to_realization(
            _load("V0010-l-b-h", "joint_graph"), envelope
        )
        canonicalize_world_realization(realization)
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry
        )
        self.assertEqual(issues, [])
        fixtures, runtime, events, reports, compile_issues = compile_world_realization(
            realization,
            envelope=envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            principal={
                "user_name": "Priya Sharma",
                "user_email": "priya@example.com",
            },
        )
        self.assertEqual(compile_issues, [])
        self.assertEqual(
            set(fixtures), {"calendar", "email_client", "zoom"}
        )
        self.assertIn("meeting", fixtures["zoom"])
        self.assertIn("events", fixtures["calendar"])
        self.assertIn("emails", fixtures["email_client"])
        self.assertTrue(runtime["prior_memory_md"])
        self.assertTrue(runtime["tree_specs"] or runtime["workspace_files"])
        self.assertNotIn("record_key", json.dumps(fixtures))
        compiled_ids = json.dumps(fixtures)
        self.assertNotRegex(compiled_ids, r"\bm\d+-")
        self.assertEqual(events, [])
        self.assertTrue(reports)

    def test_s0003_compiles_mailbox_without_volume_leakage(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        emails = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]
        self.assertEqual(len(emails["records"]), 30)
        keys = [record["record_key"] for record in emails["records"]]
        self.assertEqual(len(keys), len(set(keys)))
        fixtures, _runtime, _events, _reports, issues = compile_world_realization(
            realization,
            envelope=envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            principal={
                "user_name": "Priya Sharma",
                "user_email": "priya@example.com",
            },
        )
        self.assertEqual(issues, [])
        self.assertEqual(len(fixtures["email_client"]["emails"]), 30)
        corpus = json.dumps(fixtures["email_client"], ensure_ascii=False).lower()
        self.assertNotIn("30 emails", corpus)
        self.assertNotIn("result set too large", corpus)
        self.assertNotIn("record_key", corpus)

    def test_missing_required_model_field_is_rejected(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        del record["values"]["emails[].body"]
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry
        )
        self.assertTrue(any("missing required model fields" in issue for issue in issues))
        fixtures, _runtime, _events, _reports, compile_issues = compile_world_realization(
            realization,
            envelope=envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            principal={
                "user_name": "Priya Sharma",
                "user_email": "priya@example.com",
            },
        )
        self.assertTrue(
            any("missing required model field" in issue for issue in compile_issues)
        )
        self.assertTrue(any("emails[].body" in issue for issue in compile_issues))

    def test_multi_collection_target_and_record_ref_ids(self):
        envelope = build_fill_envelope(
            scenario_id="V0010-l-b-h",
            policy_skeleton=_load_live("V0010-l-b-h", "policy_skeleton"),
            script=_load_live("V0010-l-b-h", "script"),
            blueprint=_load_live("V0010-l-b-h", "blueprint"),
            registry=self.registry,
        )
        zoom = envelope.targets["service:o2:zoom"]
        self.assertIn("meeting.participants", zoom.scopes["meeting"].allowed_collection_paths)
        self.assertIn(
            "meeting.transcript.segments",
            zoom.scopes["meeting"].allowed_collection_paths,
        )
        template = self.registry.templates["zoom"].fixture_template
        shared = resolve_shared_values(
            template=template,
            context={"user_name": "Priya Sharma", "user_email": "priya@example.com"},
        )
        fixture, report, issues = compile_target_fixture(
            scenario_id="V0010-l-b-h",
            target_id="service:o2:zoom",
            template=template,
            scalar_values={
                "meeting.topic": "Platform weekly",
                "meeting.host": "Marcus Ely",
            },
            collections={
                "meeting.participants": {
                    "mode": "exhaustive",
                    "count": 2,
                    "records": [
                        {
                            "record_key": "priya",
                            "values": {
                                "meeting.participants[].display_name": "Priya Sharma",
                            },
                        },
                        {
                            "record_key": "marcus",
                            "values": {
                                "meeting.participants[].display_name": "Marcus Ely",
                            },
                        },
                    ],
                },
                "meeting.transcript.segments": {
                    "mode": "exhaustive",
                    "count": 1,
                    "records": [
                        {
                            "record_key": "seg-host",
                            "values": {
                                "meeting.transcript.segments[].text": "that's all I had",
                                "meeting.transcript.segments[].participant_id": {
                                    "$record_ref": {
                                        "target_id": "service:o2:zoom",
                                        "collection_path": "meeting.participants",
                                        "record_key": "priya",
                                        "field_path": "meeting.participants[].participant_id",
                                    }
                                },
                            },
                        }
                    ],
                },
            },
            shared_values=shared,
            reference_time="2026-06-06T10:11:00",
            active_roots={"meeting"},
        )
        self.assertEqual(issues, [])
        self.assertEqual(report["collections"]["meeting.participants"]["record_count"], 2)
        self.assertEqual(
            report["collections"]["meeting.transcript.segments"]["record_count"], 1
        )
        priya_id = fixture["meeting"]["participants"][0]["participant_id"]
        marcus_id = fixture["meeting"]["participants"][1]["participant_id"]
        self.assertTrue(priya_id)
        self.assertNotEqual(priya_id, marcus_id)
        self.assertEqual(
            fixture["meeting"]["transcript"]["segments"][0]["participant_id"],
            priya_id,
        )

    def test_future_output_exclusion_is_not_required_in_initial_state(self):
        envelope = _envelope(self.registry, "V0010-l-b-h")
        realization = joint_graph_to_realization(
            _load("V0010-l-b-h", "joint_graph"), envelope
        )
        emails = realization["targets"]["service:o2:email_client"][
            "collections"
        ]["emails"]["records"]
        self.assertEqual(emails, [])

    def test_leaf_keys_stay_unbound_without_classifier(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        self.assertNotIn("runtime:o1:user_chat", realization.get("runtime_targets") or {})
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        body = record["values"].pop("emails[].body")
        record["values"]["body"] = body
        audit = prepare_world_realization(realization, envelope)
        self.assertIn(
            "runtime:o1:user_chat", realization["runtime_targets"]
        )
        self.assertEqual(
            realization["runtime_targets"]["runtime:o1:user_chat"]
            ["scalar_values"]["instruction"],
            envelope.instruction,
        )
        bound_record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        self.assertEqual(bound_record["unbound_values"]["body"], body)
        self.assertNotIn("emails[].body", bound_record["values"])
        self.assertTrue(
            any(item.get("from") == "body" for item in audit["unbound"])
        )

    def test_menu_classification_binds_leaf_title_and_record_ref(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        body = record["values"].pop("emails[].body")
        subject = record["values"].pop("emails[].subject")
        record["values"]["body"] = body
        record["values"]["title"] = subject
        record["values"]["emails[].reply_to"] = {
            "$record_ref": {
                "target_id": "service:o1:email_client",
                "collection_path": "emails",
                "record_key": record["record_key"],
                "field_path": "id",
            }
        }
        audit = prepare_world_realization(
            realization,
            envelope,
            registry=self.registry,
            classify_call=_classify_wanted({
                "body": "emails[].body",
                "title": "emails[].subject",
                "id": "emails[].id",
            }),
        )
        bound = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        self.assertEqual(bound["values"]["emails[].body"], body)
        self.assertEqual(bound["values"]["emails[].subject"], subject)
        self.assertEqual(
            bound["values"]["emails[].reply_to"]["$record_ref"]["field_path"],
            "emails[].id",
        )
        self.assertNotIn("unbound_values", bound)
        classified = {
            (item["from"], item["to"]) for item in audit["classified"]
        }
        self.assertIn(("body", "emails[].body"), classified)
        self.assertIn(("title", "emails[].subject"), classified)
        self.assertIn(("id", "emails[].id"), classified)
        issues = validate_world_realization(
            realization, envelope=envelope, registry=self.registry, prepare=False
        )
        self.assertEqual(issues, [])

    def test_title_stays_unbound_until_rebind(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        subject = record["values"].pop("emails[].subject")
        record["values"]["title"] = subject
        prepare_world_realization(realization, envelope)
        bound = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        self.assertEqual(bound["unbound_values"]["title"], subject)
        self.assertNotIn("emails[].subject", bound["values"])
        from authorization_eva_sandbox.task_generation.shared.world_repair import (
            apply_world_patches,
            realization_sha256,
        )
        target = {
            "target_id": "service:o1:email_client",
            "collection_path": "emails",
            "record_key": bound["record_key"],
            "from_key": "title",
            "field_path": "emails[].subject",
        }
        patched, problems = apply_world_patches(
            realization,
            {
                "base_sha256": realization_sha256(realization),
                "patches": [{
                    "op": "rebind",
                    "target": target,
                    "current_value": subject,
                }],
            },
            allowed_targets=[target],
        )
        self.assertEqual(problems, [])
        moved = patched["targets"]["service:o1:email_client"]["collections"][
            "emails"
        ]["records"][0]
        self.assertEqual(moved["values"]["emails[].subject"], subject)
        self.assertNotIn("unbound_values", moved)

    def test_bind_authored_name_rules(self):
        allowed = ["emails[].subject", "emails[].body", "emails[].from"]
        self.assertEqual(
            bind_authored_name("emails[].body", allowed),
            ("emails[].body", "exact"),
        )
        path, reason = bind_authored_name("body", allowed)
        self.assertIsNone(path)
        self.assertEqual(reason, "unbound")
        path, reason = bind_authored_name("Subject", allowed)
        self.assertIsNone(path)
        self.assertEqual(reason, "unbound")
        path, reason = bind_authored_name("title", allowed)
        self.assertIsNone(path)
        self.assertEqual(reason, "unbound")

    def test_parse_answer_key_tolerates_formats(self):
        items = [
            ClassificationItem(
                number=1,
                kind="value_key",
                authored="body",
                place="p",
                preview="v",
                menu=[
                    MenuOption(letter="A", path="emails[].body", meaning="body"),
                    MenuOption(letter="B", path="SKIP", meaning="skip"),
                ],
                holder={},
            ),
            ClassificationItem(
                number=2,
                kind="value_key",
                authored="title",
                place="p",
                preview="v",
                menu=[
                    MenuOption(letter="A", path="emails[].subject", meaning="s"),
                    MenuOption(letter="B", path="SKIP", meaning="skip"),
                ],
                holder={},
            ),
        ]
        answers, unparsed = parse_answer_key("1A\n2: B\n", items)
        self.assertEqual(answers, {1: "A", 2: "B"})
        self.assertEqual(unparsed, [])
        answers, unparsed = parse_answer_key("#1.A\nnoise\n", items)
        self.assertEqual(answers[1], "A")
        self.assertEqual(unparsed, [2])
        self.assertEqual(
            coerce_answer_text({"answers": "1A\n2B"}),
            "1A\n2B",
        )

    def test_list_field_wraps_scalar_shared_ref(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        realization["shared_values"] = {
            "ops_list_address": "devops-list@lists.example.com",
            "principal_recipient": ["priya@example.com"],
        }
        record["values"]["emails[].to"] = {"$ref": "ops_list_address"}
        audit = prepare_world_realization(
            realization, envelope, registry=self.registry
        )
        self.assertEqual(
            record["values"]["emails[].to"],
            ["devops-list@lists.example.com"],
        )
        self.assertTrue(
            any(
                item.get("where", "").endswith("emails[].to")
                for item in audit.get("normalized") or []
            )
        )
        self.assertEqual(
            validate_world_realization(
                realization, envelope=envelope, registry=self.registry,
                prepare=False,
            ),
            [],
        )
        record["values"]["emails[].to"] = {"$ref": "principal_recipient"}
        prepare_world_realization(
            realization, envelope, registry=self.registry
        )
        self.assertEqual(
            record["values"]["emails[].to"],
            ["priya@example.com"],
        )

    def test_nested_shared_ref_in_list_is_expanded(self):
        envelope = _envelope(self.registry, "V0003-g-b-h")
        realization = joint_graph_to_realization(
            _load("V0003-g-b-h", "joint_graph"), envelope
        )
        record = realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        realization["shared_values"] = {
            "principal_email": "priya@example.com",
        }
        record["values"]["emails[].to"] = [{"$ref": "principal_email"}]
        prepare_world_realization(
            realization, envelope, registry=self.registry
        )
        self.assertEqual(
            record["values"]["emails[].to"],
            ["priya@example.com"],
        )
        fixtures, _runtime, _events, _reports, issues = compile_world_realization(
            realization,
            envelope=envelope,
            registry=self.registry,
            reference_time="2026-06-06T10:11:00",
            principal={"user_email": "priya@example.com"},
        )
        self.assertEqual(issues, [])
        self.assertEqual(
            fixtures["email_client"]["emails"][0]["to"],
            ["priya@example.com"],
        )



if __name__ == "__main__":
    unittest.main()
