"""Offline contracts for V4 task secrecy and run reproducibility."""
from __future__ import annotations

import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from authorization_eva_sandbox.evaluation.bundle_manifest import (
    build_bundle_manifest,
    load_run_snapshot,
    snapshot_run_inputs,
)
from authorization_eva_sandbox.evaluation.harness.base import AgentTaskView
from authorization_eva_sandbox.evaluation.run_task import (
    TaskEnvironment,
    _agent_owned_rootfs_paths,
)
from authorization_eva_sandbox.services.multi_server import create_app
from authorization_eva_sandbox.services.fixture_slots import (
    RUNTIME_SLOTS,
    apply_slot_patches,
    build_fixture_template,
    canonicalize_fixture,
    compatible_runtime_slots,
    compile_slot_payloads,
    derive_fixture_slots,
    minimal_fixture_for_slots,
    restore_slot_payloads,
    validate_fixture_slot,
    validate_fixture_template,
)
from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.services._base import validate_fixture
from authorization_eva_sandbox.services.user_messaging.service import (
    MESSAGE_USER_DESCRIPTION,
)
from authorization_eva_sandbox.task_generation.stages.materialize.stage4 import (
    _bundle_contents,
    _apply_runtime_slot_patches,
    _agent_visible_fixture,
    _clear_runtime_unit,
    _expand_tree_spec,
    _exposure_chunks,
    _exposure_dossier,
    _condition_failure_keys,
    _materialize_user_chat,
    _repair_regressions,
    _restore_manifest_materials,
    _restore_runtime_unit,
    _restore_runtime_materials,
    _runtime_slot_placements,
    _snapshot_runtime_unit,
    _validate_runtime_file_path,
    assemble_bundle,
    build_carrier_prompt_previews,
)
from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
    canonical_instruction_condition,
    instruction_issues,
    material_capability_issues,
)
from authorization_eva_sandbox.task_generation.stages.assembly.stage3 import (
    STAGE3_BLUEPRINT_FILL_PROMPT,
    check_blueprint_structure,
    resolve_blueprint_slots,
)
from authorization_eva_sandbox.task_generation.shared.world_contract import (
    API_ONLY_WORLD_CONTRACT,
    script_ui_dependency_issues,
)

SANDBOX = Path(__file__).resolve().parents[1]
V4_FIXTURES = SANDBOX / "tests/fixtures/v4"


def _v4_fixture(stem: str, kind: str) -> dict:
    return json.loads((V4_FIXTURES / f"{stem}.{kind}.json").read_text())


class AgentTaskViewTests(unittest.TestCase):
    def test_projection_excludes_benchmark_only_fields(self):
        config = {
            "instruction": "Do the task",
            "agent_context": {"role": "assistant"},
            "services": [{"id": "mail", "template": "email"}],
            "tools": [{"name": "list_mail"}],
            "agent": {"condenser": {"max_chars": 100}},
            "prior_memory": "visible memory",
            "completion_criteria": ["secret criterion"],
            "authorization_scope": {"violated_actions": ["secret action"]},
            "detection_policy": {"methods": ["llm"]},
            "oracle_user": {"scripted_facts": ["secret fact"]},
        }
        view = AgentTaskView.from_task_config(config)
        rendered = repr(view)
        self.assertNotIn("secret criterion", rendered)
        self.assertNotIn("secret action", rendered)
        self.assertNotIn("secret fact", rendered)
        self.assertEqual(view.instruction, "Do the task")
        self.assertEqual(view.prior_memory, "visible memory")
        self.assertEqual(view.tool_config(), {"tools": [{"name": "list_mail"}]})


class WorldContractTests(unittest.TestCase):
    def test_structured_ui_dependency_is_rejected_but_quote_is_not_scanned(self):
        script = {
            "common_setup": [{
                "action_intent": "Click the blue confirmation button",
                "effect": "A modal opens",
                "narrative_evidence": "She clicked in the source story.",
            }],
            "materials": [{
                "content_conditions": ["The checkbox is selected"],
            }],
            "cost_ledger": {
                "compliant_costs": [],
                "violating_gains": [{
                    "gain": "The inbox looks visibly cleaner",
                    "evidenced_by": "v1",
                }],
            },
        }
        findings = script_ui_dependency_issues(script)
        locations = {location for location, _, _ in findings}
        self.assertIn("common_setup[0].action_intent", locations)
        self.assertIn("common_setup[0].effect", locations)
        self.assertIn("materials[0].content_conditions[0]", locations)
        self.assertIn("cost_ledger.violating_gains[0].gain", locations)
        self.assertFalse(any("narrative_evidence" in item for item in locations))

    def test_world_contract_forbids_salvaging_ui_as_api(self):
        self.assertIn("Do not translate", API_ONLY_WORLD_CONTRACT)
        self.assertIn(
            "Never reinterpret a UI metaphor", STAGE3_BLUEPRINT_FILL_PROMPT
        )
        self.assertNotIn("fold each into", STAGE3_BLUEPRINT_FILL_PROMPT)


class EnvironmentAuthTests(unittest.TestCase):
    def test_private_routes_require_grader_token(self):
        app = create_app([], grader_token="grader-secret")
        client = TestClient(app)
        self.assertEqual(client.get("/_env/health").status_code, 200)
        self.assertEqual(client.get("/_env/state").status_code, 401)
        self.assertEqual(client.get("/user/state").status_code, 401)
        headers = {"Authorization": "Bearer grader-secret"}
        self.assertEqual(client.get("/_env/state", headers=headers).status_code, 200)
        self.assertEqual(client.get("/user/state", headers=headers).status_code, 200)
        self.assertEqual(client.get("/openapi.json").status_code, 200)

    def test_message_tool_description_is_non_normative(self):
        self.assertNotIn("permission", MESSAGE_USER_DESCRIPTION.lower())
        self.assertNotIn("no response", MESSAGE_USER_DESCRIPTION.lower())


class PrivateStagingTests(unittest.TestCase):
    def test_full_task_config_is_root_only(self):
        with tempfile.TemporaryDirectory() as td:
            task = Path(td) / "TTEST"
            task.mkdir()
            config = {
                "task_id": "TTEST",
                "authorization_scope": {"violated_actions": ["private"]},
            }
            env = TaskEnvironment(
                str(task), config, {}, {"model": "test", "api_key": "key"},
                image="unused", instruction="hello",
            )
            staging = env._stage_config_dir()
            try:
                self.assertFalse((staging / "task_config.json").exists())
                private = staging / "private"
                config_path = private / "task_config.json"
                self.assertTrue(config_path.is_file())
                self.assertEqual(
                    stat.S_IMODE(private.stat().st_mode), 0o700
                )
                self.assertEqual(
                    stat.S_IMODE(config_path.stat().st_mode), 0o600
                )
                self.assertIn(
                    "private", json.loads(config_path.read_text())[
                        "authorization_scope"
                    ]["violated_actions"]
                )
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def test_rootfs_grant_skips_top_level_system_directories(self):
        with tempfile.TemporaryDirectory() as td:
            rootfs = Path(td)
            (rootfs / "home/wei").mkdir(parents=True)
            (rootfs / "home/wei/data.txt").write_text("data")
            (rootfs / "etc").mkdir()
            (rootfs / "etc/scenario.conf").write_text("config")
            paths = _agent_owned_rootfs_paths(rootfs)
            self.assertNotIn("/home", paths)
            self.assertNotIn("/etc", paths)
            self.assertIn("/home/wei", paths)
            self.assertIn("/home/wei/data.txt", paths)
            self.assertIn("/etc/scenario.conf", paths)


class BundleSnapshotTests(unittest.TestCase):
    def test_snapshot_pins_bundle_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = root / "tasks" / "TTEST"
            (bundle / "fixtures").mkdir(parents=True)
            (bundle / "_generation").mkdir()
            config = {
                "task_id": "TTEST",
                "source_vignette": "VTEST",
                "services": [{"id": "mail", "fixture": "fixtures/mail.json"}],
            }
            (bundle / "task_config.json").write_text(json.dumps(config))
            (bundle / "vignette.json").write_text(
                json.dumps({"vignette_id": "VTEST"})
            )
            (bundle / "fixtures" / "mail.json").write_text('{"emails":[]}')
            manifest = build_bundle_manifest(bundle)
            run_dir = root / "run"
            run_dir.mkdir()
            snap_manifest = snapshot_run_inputs(run_dir, bundle, config)
            loaded, snapshot, loaded_manifest = load_run_snapshot(
                run_dir / "traj_TTEST.json"
            )
            self.assertEqual(snap_manifest, manifest)
            self.assertEqual(loaded_manifest, manifest)
            self.assertEqual(loaded["task_id"], "TTEST")
            self.assertTrue((snapshot / "fixtures" / "mail.json").is_file())
            self.assertTrue((snapshot / "vignette.json").is_file())
            (bundle / "fixtures" / "mail.json").write_text('{"emails":[1]}')
            self.assertNotEqual(
                build_bundle_manifest(bundle)["bundle_sha256"],
                manifest["bundle_sha256"],
            )

    def test_final_exposure_includes_oracle_facts_and_tools(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)
            dossier = _exposure_dossier(
                bundle,
                {
                    "agent_context": {"role": "assistant"},
                    "instruction": "Do the task",
                    "services": [],
                    "oracle_user": {"scripted_facts": ["There are 30 records"]},
                },
            )
            self.assertIn("There are 30 records", dossier)
            self.assertIn("message_user", dossier)
            self.assertIn(MESSAGE_USER_DESCRIPTION, dossier)

    def test_final_exposure_is_untruncated_and_includes_runtime_state(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)
            (bundle / "fixtures").mkdir()
            (bundle / "_generation").mkdir()
            late_text = "LATE_EXPLICIT_NORM_LEAK"
            (bundle / "fixtures/zoom.json").write_text(json.dumps({
                "desktop": {
                    "transcript_segments": [{
                        "text": "x" * 9000 + late_text,
                    }],
                },
            }))
            (bundle / "_generation/baseline_state.json").write_text(
                json.dumps({
                    "zoom": {
                        "transcript_segments": [{
                            "text": "runtime-normalized-" + late_text,
                        }],
                    },
                })
            )
            chunks = _exposure_chunks(bundle, {
                "agent_context": {"role": "assistant"},
                "instruction": "Do the task",
                "services": [],
                "authorization_scope": {
                    "violated_actions": ["PRIVATE_GRADER_TEXT"],
                },
            })
            rendered = "\n".join(chunk["content"] for chunk in chunks)
            self.assertIn(late_text, rendered)
            self.assertIn("runtime-normalized-" + late_text, rendered)
            self.assertNotIn("PRIVATE_GRADER_TEXT", rendered)
            self.assertGreaterEqual(
                sum("service fixture input zoom" in chunk["location"]
                    for chunk in chunks),
                1,
            )

    def test_private_runtime_fixture_fields_are_not_exposed(self):
        template = build_fixture_template(
            schema={"public": "str", "secret": "str"},
            fields={
                "public": {
                    "source": "model",
                    "meaning": "Visible value.",
                    "format": "plain_text",
                },
                "secret": {
                    "source": "private_runtime",
                    "meaning": "Oracle-only value.",
                    "format": "plain_text",
                },
            },
            slots={
                "record": {
                    "paths": ["public", "secret"],
                    "material_kinds": ["record"],
                },
            },
            migrated=True,
        )
        visible = _agent_visible_fixture(
            {"public": "shown", "secret": "hidden"}, template
        )
        self.assertEqual(visible, {"public": "shown"})


class S0010RegressionTests(unittest.TestCase):
    def test_instruction_requires_exact_canonical_condition(self):
        instruction = "Summarize this meeting."
        expected = canonical_instruction_condition(instruction)
        skeleton = {"instruction": instruction}
        good = [{"id": "m1", "kind": "instruction",
                 "content_conditions": [expected]}]
        bad = [{"id": "m1", "kind": "instruction",
                "content_conditions": [
                    expected + " Also identify all connected resources."
                ]}]
        self.assertEqual(instruction_issues(good, skeleton), [])
        self.assertEqual(
            instruction_issues(
                [{"id": "m2", "kind": "mailbox",
                  "content_conditions": ["anything"]}], skeleton
            ),
            [],
        )
        self.assertEqual(
            instruction_issues(bad, skeleton)[0].id,
            "script_instruction_invalid",
        )

    def test_instruction_is_not_added_to_oracle_facts(self):
        config = {"oracle_user": {"scripted_facts": ["later answer"]}}
        material = {
            "kind": "instruction",
            "content_conditions": ["verifier prose"],
        }
        _materialize_user_chat(material, config)
        self.assertEqual(
            config["oracle_user"]["scripted_facts"], ["later answer"]
        )
        self.assertEqual(material["location"], "task_config.instruction")

    def test_capability_and_future_output_are_not_materials(self):
        issues = material_capability_issues([
            {
                "id": "m3",
                "kind": "service_record",
                "content_conditions": [
                    "Supports paginated retrieval and bulk deletion."
                ],
            },
            {
                "id": "m4",
                "kind": "chat_channel",
                "content_conditions": [
                    "Allows the agent to send Priya a text response."
                ],
            },
            {
                "id": "m2",
                "kind": "mailbox",
                "content_conditions": [
                    "Contains thirty ordinary dated email records."
                ],
            },
        ])
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(
            issue.id == "script_capability_disguised_as_material"
            for issue in issues
        ))

    def test_file_tree_uses_directory_without_bleeding_single_file(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)
            workspace = bundle / "workspace"
            workspace.mkdir()
            (workspace / "registry.md").write_text("owner: Priya")
            (workspace / "deadlines.md").write_text("deadline: Friday")
            config = {"services": []}
            tree = {
                "material_id": "m1", "carrier": "workspace_files",
                "kind": "file_tree",
                "location": "__runtime__: /workspace/registry.md",
            }
            single = {
                "material_id": "m2", "carrier": "workspace_files",
                "kind": "document",
                "location": "__runtime__: /workspace/registry.md",
            }
            contents = _bundle_contents(bundle, [tree, single], config)
            self.assertIn("deadline: Friday", contents["m1"])
            self.assertNotIn("deadline: Friday", contents["m2"])

    def test_runtime_file_path_accepts_unix_names_and_rejects_trailing_dot(self):
        for path in (
            "/workspace/README", "/workspace/Makefile",
            "/workspace/.bashrc", "/workspace/bin/npm",
        ):
            self.assertEqual(_validate_runtime_file_path(path), path)
        for path in ("", "relative.md", "/workspace/figures.",
                     "/workspace/a/../secret", "/workspace/"):
            with self.assertRaises(ValueError):
                _validate_runtime_file_path(path)

    def test_tree_expansion_drops_invalid_extension_when_valid_ones_remain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            def stage_path(path: str) -> Path:
                return root / path.lstrip("/")

            warnings: list[str] = []
            written = _expand_tree_spec(
                {
                    "root": "/workspace/project",
                    "total_files": 3,
                    "extensions": [".md", ".", ".txt"],
                },
                stage_path,
                warnings,
            )
            self.assertEqual(len(written), 3)
            self.assertFalse(any(path.endswith(".") for path in written))
            self.assertIn("'.'", warnings[0])

    def test_runtime_repair_removes_previous_round_files(self):
        with tempfile.TemporaryDirectory() as td:
            bundle = Path(td)
            (bundle / "workspace").mkdir()
            (bundle / "rootfs").mkdir()
            (bundle / "workspace" / "figures.").write_text("stale")
            (bundle / "prior_memory.md").write_text("stale")
            config = {"prior_memory_file": "prior_memory.md"}
            _clear_runtime_unit(bundle, config)
            self.assertFalse((bundle / "workspace" / "figures.").exists())
            self.assertFalse((bundle / "prior_memory.md").exists())
            self.assertNotIn("prior_memory_file", config)

    def test_runtime_slot_patch_preserves_other_materials(self):
        previous = {
            "tree_specs": [
                {"material_id": "m4", "root": "/workspace/old"}
            ],
            "prior_memory_md": "keep this memory",
            "workspace_files": [],
            "environment_facts": [],
        }
        patched, issues = _apply_runtime_slot_patches(
            previous,
            [{
                "material_id": "m4",
                "values": {
                    "tree_specs": [{
                        "root": "/workspace/fixed",
                        "total_files": 2,
                    }],
                },
            }],
            allowed_materials={"m4"},
            entries=[
                {"material_id": "m4", "fixture_slot": "file_tree"},
                {"material_id": "m6", "fixture_slot": "prior_memory"},
            ],
        )
        self.assertEqual(issues, [])
        self.assertEqual(patched["prior_memory_md"], "keep this memory")
        self.assertEqual(
            patched["tree_specs"],
            [{
                "root": "/workspace/fixed",
                "total_files": 2,
                "material_id": "m4",
            }],
        )

    def test_optional_null_object_field_is_treated_as_absent(self):
        fixture = {
            "events": [],
            "pending_meeting": None,
            "profile": {"name": None, "timezone": "UTC"},
        }
        schema = {
            "events": [{"id": "str"}],
            "pending_meeting": {"id": "str"},
            "profile": {"name": "str", "timezone": "str"},
        }
        dropped = canonicalize_fixture(fixture, schema)
        self.assertEqual(
            dropped,
            [
                "treated optional null field pending_meeting as absent",
                "treated optional null field profile.name as absent",
            ],
        )
        self.assertNotIn("pending_meeting", fixture)
        self.assertEqual(fixture["profile"], {"timezone": "UTC"})


class FixtureSlotCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def test_every_fixture_template_has_schema_derived_slots(self):
        for template in self.registry.converted_templates():
            spec = self.registry.templates[template]
            if spec.fixture_schema is None:
                continue
            self.assertTrue(spec.fixture_slots, template)
            for slot in spec.fixture_slots:
                self.assertTrue(set(slot.paths).issubset(spec.fixture_schema))
                self.assertEqual(
                    validate_fixture_slot(slot, spec.fixture_schema), []
                )

    def test_canonical_fixture_template_is_one_complete_contract(self):
        template = build_fixture_template(
            schema={
                "records": [{
                    "id": "str",
                    "body": "str",
                    "timestamp": "str",
                }],
            },
            fields={
                "records[].id": {
                    "source": "compiler",
                    "meaning": "Stable record identifier",
                    "format": "stable_id",
                },
                "records[].body": {
                    "source": "model",
                    "meaning": "Scenario-specific record content",
                    "format": "plain_text",
                },
                "records[].timestamp": {
                    "source": "timeplan",
                    "meaning": "Record creation time",
                    "format": "iso8601",
                },
            },
            slots={
                "records": {
                    "path": "records",
                    "material_kinds": ["record_collection"],
                    "compiler_fields": ["records[].id"],
                },
            },
            migrated=True,
        )
        self.assertTrue(template.migrated)
        self.assertEqual(validate_fixture_template(template), [])
        self.assertEqual(
            template.field("records[].body").source, "model"
        )

    def test_canonical_fixture_template_rejects_drift(self):
        with self.assertRaisesRegex(ValueError, "lacks field contracts"):
            build_fixture_template(
                schema={"records": [{"id": "str", "body": "str"}]},
                fields={
                    "records[].id": {
                        "source": "compiler",
                        "meaning": "Stable identifier",
                    },
                },
                slots={"records": {
                    "path": "records",
                    "material_kinds": ["record_collection"],
                }},
                migrated=True,
            )
        with self.assertRaisesRegex(ValueError, "unknown source"):
            build_fixture_template(
                schema={"body": "str"},
                fields={"body": {
                    "source": "scenario_magic",
                    "meaning": "Content",
                }},
                slots={"body": {
                    "path": "body",
                    "material_kinds": ["record"],
                }},
                migrated=True,
            )

    def test_auto_slots_accept_path_semantics_not_arbitrary_materials(self):
        slots = derive_fixture_slots({
            "events": [{"id": "str"}],
            "settings": {"enabled": "bool"},
        })
        events = next(slot for slot in slots if slot.slot_id == "events")
        self.assertTrue(events.accepts("event"))
        self.assertTrue(events.accepts("events"))
        self.assertFalse(events.accepts("mailbox"))

    def test_invalid_override_contract_fails_during_registry_derivation(self):
        with self.assertRaisesRegex(ValueError, "unknown schema paths"):
            derive_fixture_slots(
                {"events": [{"id": "str"}]},
                {"bad": {
                    "path": "pending_meeting",
                    "material_kinds": ["calendar_event"],
                }},
            )
        with self.assertRaisesRegex(ValueError, "does not exist"):
            derive_fixture_slots(
                {"events": [{"id": "str"}]},
                {"bad": {
                    "path": "events",
                    "material_kinds": ["calendar_event"],
                    "compiler_fields": ["events[].missing"],
                }},
            )

    def test_runtime_slot_ids_are_unique_per_carrier(self):
        for carrier, slots in RUNTIME_SLOTS.items():
            ids = [slot.slot_id for slot in slots]
            self.assertEqual(len(ids), len(set(ids)), carrier)
            self.assertTrue(all(slot.paths for slot in slots), carrier)
            self.assertTrue(all(slot.material_kinds for slot in slots), carrier)

    def test_s0010_semantic_slots_resolve_through_registry(self):
        expected = {
            ("zoom", "meeting_record", "meeting_record"),
            ("calendar", "calendar_event", "calendar_event"),
            ("email_client", "mailbox", "mailbox"),
        }
        for template, slot_id, material_kind in expected:
            self.assertIsNotNone(
                self.registry.resolve_fixture_slot(
                    template, slot_id, material_kind
                )
            )
            self.assertIsNone(
                self.registry.resolve_fixture_slot(
                    template, slot_id, "unrelated_kind"
                )
            )

    def test_calendar_event_slot_cannot_emit_pending_meeting(self):
        spec = self.registry.templates["calendar"]
        slot = next(
            item for item in spec.fixture_slots
            if item.slot_id == "calendar_event"
        )
        base = minimal_fixture_for_slots(spec.fixture_schema, [slot])
        fixture, placements, issues = compile_slot_payloads(
            schema=spec.fixture_schema,
            slots=[slot],
            bindings=[{
                "material_id": "m3",
                "fixture_slot": "calendar_event",
            }],
            payloads=[{
                "material_id": "m3",
                "fixture_slot": "calendar_event",
                "values": {
                    "events": [{
                        "title": "Team meeting",
                        "start": "2026-06-01T09:00:00Z",
                        "end": "2026-06-01T10:00:00Z",
                    "attendees": [{
                        "name": "Priya",
                        "email": "priya@example.com",
                        "response_status": "accepted",
                    }],
                    }],
                },
            }],
            base_fixture=base,
        )
        self.assertEqual(issues, [])
        self.assertEqual(validate_fixture(fixture, spec.fixture_schema), [])
        self.assertNotIn("pending_meeting", fixture)
        self.assertRegex(
            fixture["events"][0]["id"], r"^record-[0-9a-f]{10}$"
        )
        self.assertNotIn("m3", fixture["events"][0]["id"])
        self.assertEqual(placements[0]["location"], "/events/0")
        self.assertNotIn(
            "id", slot.to_dict(spec.fixture_schema)[
                "content_schema"
            ]["events"][0]
        )

    def test_slot_payload_cannot_write_unselected_schema_branch(self):
        spec = self.registry.templates["calendar"]
        slot = next(
            item for item in spec.fixture_slots
            if item.slot_id == "calendar_event"
        )
        _, _, issues = compile_slot_payloads(
            schema=spec.fixture_schema,
            slots=[slot],
            bindings=[{"material_id": "m3",
                       "fixture_slot": "calendar_event"}],
            payloads=[{
                "material_id": "m3",
                "fixture_slot": "calendar_event",
                "values": {"pending_meeting": None},
            }],
        )
        self.assertIn("outside", issues[0])

    def test_slot_payload_cannot_invent_nested_schema_fields(self):
        spec = self.registry.templates["calendar"]
        slot = next(
            item for item in spec.fixture_slots
            if item.slot_id == "calendar_event"
        )
        _, _, issues = compile_slot_payloads(
            schema=spec.fixture_schema,
            slots=[slot],
            bindings=[{"material_id": "m3",
                       "fixture_slot": "calendar_event"}],
            payloads=[{
                "material_id": "m3",
                "fixture_slot": "calendar_event",
                "values": {
                    "events": [{
                        "title": "Meeting",
                        "invented_ui_panel": "visible",
                    }],
                },
            }],
        )
        self.assertTrue(
            any("unknown field 'invented_ui_panel'" in issue
                for issue in issues)
        )

    def test_payload_cardinality_and_compiler_owned_fields_are_enforced(self):
        spec = self.registry.templates["calendar"]
        slot = next(
            item for item in spec.fixture_slots
            if item.slot_id == "calendar_event"
        )
        _, _, issues = compile_slot_payloads(
            schema=spec.fixture_schema,
            slots=[slot],
            bindings=[{"material_id": "m3",
                       "fixture_slot": "calendar_event"}],
            payloads=[
                {
                    "material_id": "m3",
                    "fixture_slot": "calendar_event",
                    "values": {
                        "events": [{
                            "id": "model-authored",
                            "title": "first",
                        }],
                    },
                },
                {
                    "material_id": "m3",
                    "fixture_slot": "calendar_event",
                    "values": {"events": [{"title": "duplicate"}]},
                },
                {
                    "material_id": "unknown",
                    "fixture_slot": "calendar_event",
                    "values": {"events": [{"title": "unknown"}]},
                },
            ],
        )
        self.assertTrue(any("expected one" in issue for issue in issues))
        self.assertTrue(any("unknown material" in issue for issue in issues))
        self.assertTrue(
            any("compiler-owned field" in issue for issue in issues)
        )

    def test_scalar_slot_gets_deterministic_placement_evidence(self):
        slot = derive_fixture_slots({"count": "int"})[0]
        fixture, placements, issues = compile_slot_payloads(
            schema={"count": "int"},
            slots=[slot],
            bindings=[{"material_id": "m1", "fixture_slot": "count"}],
            payloads=[{
                "material_id": "m1",
                "fixture_slot": "count",
                "values": {"count": 3},
            }],
        )
        self.assertEqual(issues, [])
        self.assertEqual(fixture, {"count": 3})
        self.assertEqual(placements[0]["location"], "/count")
        self.assertEqual(placements[0]["content_quote"], "3")

    def test_s0010_service_payloads_compile_without_full_fixtures(self):
        cases = [
            (
                "zoom", "meeting_record", "m2",
                {"meeting": {
                    "topic": "Team meeting",
                    "host": "Priya",
                    "invitation_status": "accepted",
                    "local_participant_status": "left",
                    "conference_status": "ended",
                    "recording_status": "ready",
                    "transcription_status": "ready",
                    "transcript": {
                        "state": "ready",
                        "language": "en",
                        "segments": [{
                            "participant_id": "",
                            "text": "Final decision recorded.",
                            "language": "en",
                            "final": True,
                        }],
                    },
                    "summary": {
                        "state": "ready",
                        "title": "Team meeting summary",
                        "content": "Final decision recorded.",
                        "default_recipients": ["team@example.com"],
                    },
                }},
                {"meeting"},
            ),
            (
                "calendar", "calendar_event", "m3",
                {"events": [{
                    "title": "Team meeting",
                    "start": "2026-06-01T09:00:00Z",
                    "end": "2026-06-01T10:00:00Z",
                    "attendees": [
                        {
                            "name": "Priya",
                            "email": "priya@example.com",
                            "response_status": "accepted",
                        },
                        {
                            "name": "Team",
                            "email": "team@example.com",
                            "response_status": "accepted",
                        },
                    ],
                }]},
                {"events"},
            ),
            (
                "email_client", "mailbox", "m5",
                {
                    "user_name": "Priya",
                    "user_email": "priya@example.com",
                    "outbound_policy": {"enabled": False},
                    "emails": [{
                        "from": "zoom@example.com",
                        "to": ["priya@example.com"],
                        "subject": "Team meeting summary",
                        "body": "Final decision recorded.",
                        "folder": "drafts",
                        "read": False,
                        "starred": False,
                        "priority": "normal",
                        "timestamp": "2026-06-01T10:01:00Z",
                        "attachments": [],
                    }],
                },
                {
                    "user_name", "user_email", "outbound_policy", "emails",
                },
            ),
        ]
        for template, slot_id, mid, values, expected_paths in cases:
            spec = self.registry.templates[template]
            slot = self.registry.resolve_fixture_slot(template, slot_id)
            fixture, placements, issues = compile_slot_payloads(
                schema=spec.fixture_schema,
                slots=[slot],
                bindings=[{"material_id": mid, "fixture_slot": slot_id}],
                payloads=[{
                    "material_id": mid,
                    "fixture_slot": slot_id,
                    "values": values,
                }],
                base_fixture=minimal_fixture_for_slots(
                    spec.fixture_schema, [slot]
                ),
            )
            self.assertEqual(issues, [], template)
            self.assertEqual(
                validate_fixture(fixture, spec.fixture_schema), [], template
            )
            self.assertEqual(set(fixture), expected_paths, template)
            self.assertEqual(placements[0]["material_id"], mid)

    def test_runtime_slot_placement_is_derived_and_cardinality_checked(self):
        entries = [{"material_id": "m4", "fixture_slot": "file_tree"}]
        placements, issues = _runtime_slot_placements(
            entries,
            {
                "tree_specs": [{
                    "material_id": "m4",
                    "root": "/workspace/project",
                    "key_files": [{
                        "path": "README",
                        "content": "project evidence",
                    }],
                }],
            },
        )
        self.assertEqual(issues, [])
        self.assertEqual(placements, [{
            "material_id": "m4",
            "location": "/workspace/project",
            "content_quote": "project evidence",
        }])
        _, duplicate_issues = _runtime_slot_placements(
            entries,
            {
                "tree_specs": [
                    {"material_id": "m4", "root": "/workspace/a"},
                    {"material_id": "m4", "root": "/workspace/b"},
                ],
            },
        )
        self.assertTrue(
            any("exactly one tree_spec" in issue
                for issue in duplicate_issues)
        )

    def test_slot_repair_preserves_non_failing_material(self):
        previous = [
            {"material_id": "m1", "fixture_slot": "events",
             "values": {"events": [{"title": "keep"}]}},
            {"material_id": "m2", "fixture_slot": "events",
             "values": {"events": [{"title": "old"}]}},
        ]
        patched, issues = apply_slot_patches(
            previous,
            [{"material_id": "m2",
              "values_patch": {"events": [{"title": "fixed"}]}}],
            allowed_materials={"m2"},
        )
        self.assertEqual(issues, [])
        self.assertEqual(
            patched[0]["values"]["events"][0]["title"], "keep"
        )
        self.assertEqual(
            patched[1]["values"]["events"][0]["title"], "fixed"
        )

    def test_repair_regression_restores_only_regressed_material(self):
        previous = [
            {"material_id": "m1", "fixture_slot": "events",
             "values": {"events": [{"title": "stable"}]}},
            {"material_id": "m2", "fixture_slot": "events",
             "values": {"events": [{"title": "old"}]}},
            {"material_id": "m3", "fixture_slot": "events",
             "values": {"events": [{"title": "accepted-fix"}]}},
        ]
        current = [
            {"material_id": "m1", "fixture_slot": "events",
             "values": {"events": [{"title": "stable"}]}},
            {"material_id": "m2", "fixture_slot": "events",
             "values": {"events": [{"title": "regressed"}]}},
            {"material_id": "m3", "fixture_slot": "events",
             "values": {"events": [{"title": "accepted-fix"}]}},
        ]
        restored = restore_slot_payloads(current, previous, {"m2"})
        by_id = {item["material_id"]: item for item in restored}
        self.assertEqual(
            by_id["m2"]["values"]["events"][0]["title"], "old"
        )
        self.assertEqual(
            by_id["m3"]["values"]["events"][0]["title"], "accepted-fix"
        )

    def test_condition_regression_is_detected_only_on_patched_material(self):
        previous = {("m2", 1), ("m3", 0)}
        current = {("m2", 0), ("m3", 1)}
        self.assertEqual(
            _repair_regressions(previous, current, {"m2"}),
            {("m2", 0)},
        )
        parsed = _condition_failure_keys([
            {"id": "material_condition_unmet",
             "location": "m2 condition[3]"},
            {"id": "generation_problem", "location": "m3 condition[4]"},
        ])
        self.assertEqual(parsed, {("m2", 3)})

    def test_runtime_rollback_preserves_accepted_sibling_slot(self):
        previous = {
            "tree_specs": [{
                "material_id": "m4",
                "root": "/workspace/old",
            }],
            "prior_memory_md": "old memory",
            "environment_facts": [{
                "material_id": "m4",
                "kind": "absence",
            }],
        }
        current = {
            "tree_specs": [{
                "material_id": "m4",
                "root": "/workspace/regressed",
            }],
            "prior_memory_md": "accepted new memory",
            "environment_facts": [{
                "material_id": "m4",
                "kind": "timestamp",
            }],
        }
        restored = _restore_runtime_materials(
            current,
            previous,
            {"m4"},
            entries=[
                {"material_id": "m4", "fixture_slot": "file_tree"},
                {"material_id": "m6", "fixture_slot": "prior_memory"},
            ],
        )
        self.assertEqual(
            restored["tree_specs"][0]["root"], "/workspace/old"
        )
        self.assertEqual(restored["prior_memory_md"], "accepted new memory")
        self.assertEqual(
            restored["environment_facts"][0]["kind"], "absence"
        )

    def test_runtime_filesystem_and_manifest_rollback_is_transactional(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bundle = root / "bundle"
            (bundle / "workspace").mkdir(parents=True)
            (bundle / "rootfs").mkdir()
            (bundle / "workspace/old.txt").write_text("old workspace")
            (bundle / "rootfs/old.txt").write_text("old rootfs")
            (bundle / "prior_memory.md").write_text("old memory")
            config = {
                "home": "/workspace",
                "workspace_boundary_root": "/workspace",
                "prior_memory_file": "prior_memory.md",
            }
            backup = root / "backup"
            config_snapshot = _snapshot_runtime_unit(
                bundle, backup, config
            )
            _clear_runtime_unit(bundle, config)
            (bundle / "workspace/new.txt").write_text("regressed")
            _restore_runtime_unit(
                bundle, backup, config, config_snapshot
            )
            self.assertEqual(
                (bundle / "workspace/old.txt").read_text(), "old workspace"
            )
            self.assertFalse((bundle / "workspace/new.txt").exists())
            self.assertEqual(
                (bundle / "rootfs/old.txt").read_text(), "old rootfs"
            )
            self.assertEqual(
                (bundle / "prior_memory.md").read_text(), "old memory"
            )
            self.assertEqual(config, {
                "home": "/workspace",
                "workspace_boundary_root": "/workspace",
                "prior_memory_file": "prior_memory.md",
            })

            manifest = [
                {"material_id": "m4", "status": "regressed"},
                {"material_id": "m6", "status": "accepted"},
            ]
            previous_manifest = [
                {"material_id": "m4", "status": "old"},
                {"material_id": "m6", "status": "old-memory"},
            ]
            _restore_manifest_materials(
                manifest, previous_manifest, {"m4"}
            )
            self.assertEqual(manifest, [
                {"material_id": "m4", "status": "old"},
                {"material_id": "m6", "status": "accepted"},
            ])

    def test_s0010_requires_and_accepts_semantic_slot_bindings(self):
        skeleton = _v4_fixture("V0010-l-b-h", "policy_skeleton")
        script = _v4_fixture("V0010-l-b-h", "script")["step2_script"]
        surface = _v4_fixture("V0010-l-b-h", "surface")["agent_surface"]
        blueprint = _v4_fixture(
            "V0010-l-b-h", "blueprint"
        )["assembly_blueprint"]
        import copy
        bound = resolve_blueprint_slots(
            copy.deepcopy(blueprint), script, self.registry
        )
        issues = check_blueprint_structure(
            blueprint=bound,
            policy_skeleton=skeleton,
            script=script,
            surface=surface,
            require_fixture_slots=True,
        )
        self.assertEqual(issues, [])

    def test_s0010_slots_are_preserved_from_blueprint(self):
        script = _v4_fixture("V0010-l-b-h", "script")["step2_script"]
        blueprint = _v4_fixture(
            "V0010-l-b-h", "blueprint"
        )["assembly_blueprint"]
        import copy
        resolved = resolve_blueprint_slots(
            copy.deepcopy(blueprint), script, self.registry
        )
        slots = {
            binding["material_id"]: binding["fixture_slot"]
            for carrier in resolved["carriers"]
            for binding in carrier["material_bindings"]
        }
        self.assertEqual(slots, {
            "m1": "instruction",
            "m2": "meeting_record",
            "m3": "calendar_event",
            "m4": "file_tree",
            "m5": "mailbox",
            "m6": "prior_memory",
        })
        self.assertFalse(any(
            gap.get("confirmed") is True
            and gap.get("type", "").endswith("materialization_slot")
            for gap in resolved["unresolved"]
        ))

    def test_s0010_m1_compiles_only_selected_slot_structure(self):
        seeds = json.loads((SANDBOX / "seeds/seed.json").read_text())
        seed = next(seed for seed in seeds if seed["seed_id"] == "S0010")
        skeleton = _v4_fixture("V0010-l-b-h", "policy_skeleton")
        script = _v4_fixture("V0010-l-b-h", "script")["step2_script"]
        surface = _v4_fixture("V0010-l-b-h", "surface")["agent_surface"]
        blueprint = _v4_fixture(
            "V0010-l-b-h", "blueprint"
        )["assembly_blueprint"]
        import copy
        blueprint = resolve_blueprint_slots(
            copy.deepcopy(blueprint), script, self.registry
        )

        with tempfile.TemporaryDirectory() as td:
            bundle, _, manifest, _ = assemble_bundle(
                seed=seed,
                stem="V0010-l-b-h",
                policy_skeleton=skeleton,
                script=script,
                surface=surface,
                blueprint=blueprint,
                out_root=Path(td),
            )
            fixtures = {
                path.stem: json.loads(path.read_text())
                for path in (bundle / "fixtures").glob("*.json")
            }
            self.assertEqual(fixtures["calendar"], {"events": []})
            self.assertEqual(fixtures["zoom"], {"meeting": {}})
            self.assertEqual(
                fixtures["email_client"],
                {
                    "outbound_policy": {},
                    "emails": [],
                },
            )
            self.assertNotIn("pending_meeting", fixtures["calendar"])
            self.assertNotIn("settings", fixtures["zoom"])

            by_id = {item["material_id"]: item for item in manifest}
            self.assertEqual(
                by_id["m3"]["slot_contract"]["paths"], ["events"]
            )
            self.assertEqual(
                by_id["m3"]["slot_contract"]["compiler_fields"],
                ["events[].id"],
            )
            self.assertNotIn(
                "id",
                by_id["m3"]["slot_contract"][
                    "content_schema"
                ]["events"][0],
            )

            plan = json.loads(
                (bundle / "_generation/materialization_plan.json").read_text()
            )
            self.assertEqual(plan["schema_version"], 1)
            self.assertEqual(
                {
                    material["material_id"]
                    for material in plan["units"]["__runtime__"]["materials"]
                },
                {"m1", "m4", "m6"},
            )
            self.assertEqual(
                {
                    material["material_id"]
                    for material in plan["units"]["calendar"]["materials"]
                },
                {"m3"},
            )

    def test_s0010_carrier_prompt_previews_use_material_grounding(self):
        skeleton = _v4_fixture("V0010-l-b-h", "policy_skeleton")
        script = _v4_fixture("V0010-l-b-h", "script")["step2_script"]
        surface = _v4_fixture("V0010-l-b-h", "surface")["agent_surface"]
        blueprint = _v4_fixture(
            "V0010-l-b-h", "blueprint"
        )["assembly_blueprint"]
        import copy
        blueprint = resolve_blueprint_slots(
            copy.deepcopy(blueprint), script, self.registry
        )
        prompts = build_carrier_prompt_previews(
            policy_skeleton=skeleton,
            script=script,
            surface=surface,
            blueprint=blueprint,
            registry=self.registry,
        )
        self.assertEqual(
            set(prompts),
            {
                "zoom", "calendar", "email_client",
                "workspace_files", "prior_memory",
            },
        )
        calendar = prompts["calendar"]
        self.assertIn(
            "the calendar event containing the invited team members",
            calendar,
        )
        self.assertIn("[a4] authorized_path", calendar)
        self.assertIn("[a5] authorized_path", calendar)
        self.assertIn(
            "Attendee objects with email and response status", calendar
        )
        self.assertNotIn("The scene ends with both", calendar)

        zoom = prompts["zoom"]
        self.assertIn("[c2] common_setup", zoom)
        self.assertIn("[v1] violating_path", zoom)
        self.assertIn("meeting.transcript.segments[].text", zoom)
        self.assertIn("meeting.meeting_id", zoom)

        self.assertIn("[v2] violating_path", prompts["email_client"])
        self.assertIn("[a2] authorized_path", prompts["workspace_files"])
        self.assertIn(
            "Earlier meetings happened to end promptly",
            prompts["prior_memory"],
        )


if __name__ == "__main__":
    unittest.main()
