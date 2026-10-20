from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.world_fill_envelope import (
    FillEnvelopeError,
    build_fill_envelope,
    require_stage3_registry_match,
)
from authorization_eva_sandbox.evaluation.bundle_manifest import (
    GENERATION_INPUTS,
    build_bundle_manifest,
)
from authorization_eva_sandbox.task_generation.shared.world_stage4 import (
    RAW_REALIZATION_NAME,
    attempt_dir,
    begin_stage4_attempt,
    materialize_world_offline,
    persist_stage4_validation,
    persist_stage4_text_audit,
    require_stage4_receipt,
    run_stage4_repair_loop,
    seal_stage4_bundle,
)
from authorization_eva_sandbox.task_generation.shared.world_realization import (
    validate_world_realization,
)
from authorization_eva_sandbox.tests.test_world_fill_envelope import _load, _load_live
from authorization_eva_sandbox.tests.test_world_realization_contract import (
    joint_graph_to_realization,
)


def _replacement(text: str) -> str:
    return f"REPLACEMENT\n{text}\nEND REPLACEMENT"


class Stage4OrchestrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()
        cls.seed = {"seed_id": "S0003", "situational_trigger": ["g"]}
        cls.stem = "V0003-g-b-h"
        cls.envelope = build_fill_envelope(
            scenario_id=cls.stem,
            policy_skeleton=_load(cls.stem, "policy_skeleton"),
            script=_load(cls.stem, "script"),
            blueprint=_load(cls.stem, "blueprint"),
            registry=cls.registry,
        )
        cls.realization = joint_graph_to_realization(
            _load(cls.stem, "joint_graph"), cls.envelope
        )
        cls.policy_skeleton = _load(cls.stem, "policy_skeleton")

    def _materialize(self, out_root, attempt_id, realization=None, **extra):
        return materialize_world_offline(
            out_root=out_root,
            seed=self.seed,
            stem=self.stem,
            policy_skeleton=self.policy_skeleton,
            script=_load(self.stem, "script"),
            surface=_load(self.stem, "surface"),
            blueprint=_load(self.stem, "blueprint"),
            realization=realization if realization is not None else self.realization,
            registry=self.registry,
            attempt_id=attempt_id,
            **extra,
        )

    def _body_finding(self, *, repairable: bool = True) -> dict:
        record = self.realization["targets"]["service:o1:email_client"][
            "collections"
        ]["emails"]["records"][0]
        address = {
            "target_id": "service:o1:email_client",
            "collection_path": "emails",
            "record_key": record["record_key"],
            "field_path": "emails[].body",
        }
        display = " > ".join(str(address[key]) for key in address)
        return {
            "where": f"m2[0] @ {display}",
            "problem": "body does not satisfy the inbox condition",
            "suggestion": "rewrite the body without leaking the boundary",
            "material_id": "m2",
            "condition_index": 0,
            "location": {
                "display": display,
                "kind": "field",
                "address": address,
                "repairable": repairable,
            },
        }

    def test_failed_attempt_does_not_replace_canonical(self):
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td)
            bundle, report = self._materialize(
                out_root,
                "good",
                inject_passing_gates=True,
            )
            self.assertTrue(report["pass"])
            marker = bundle / "task_config.json"
            original = marker.read_text()
            broken = json.loads(json.dumps(self.realization))
            del broken["targets"]["service:o1:email_client"]["collections"][
                "emails"
            ]["records"][0]["values"]["emails[].body"]
            staging, failed = self._materialize(
                out_root,
                "bad",
                realization=broken,
                promote=False,
            )
            self.assertFalse(failed["pass"])
            self.assertTrue(staging.exists())
            self.assertEqual(marker.read_text(), original)
            self.assertTrue(
                (attempt_dir(out_root, "T0003-g-b-h", "bad") / "_generation"
                 / "validation.json").is_file()
            )

    def test_materialization_copies_canonical_vignette_into_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            canonical = {
                "schema_version": 1,
                "vignette_id": self.stem,
                "instruction": "Check my inbox.",
            }
            staging, report = self._materialize(
                Path(td),
                "with-vignette",
                canonical_vignette=canonical,
                promote=False,
                seal=False,
            )
            self.assertTrue(report["pass"])
            self.assertEqual(
                json.loads((staging / "vignette.json").read_text()),
                canonical,
            )

    def test_validation_failure_retains_parseable_dump_without_touching_canonical(self):
        broken = json.loads(json.dumps(self.realization))
        del broken["targets"]["service:o1:email_client"]["collections"][
            "emails"
        ]["records"][0]["values"]["emails[].body"]
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td)
            canonical = out_root / "T0003-g-b-h"
            canonical.mkdir()
            marker = canonical / "task_config.json"
            marker.write_text('{"canonical": true}\n')
            original = marker.read_text()
            staging = begin_stage4_attempt(
                out_root,
                "T0003-g-b-h",
                attempt_id="validate-fail",
                realization=broken,
            )
            issues = validate_world_realization(
                broken, envelope=self.envelope, registry=self.registry
            )
            self.assertTrue(issues)
            persist_stage4_validation(staging, issues)
            dumped = json.loads(
                (staging / "_generation" / "world_realization.json").read_text()
            )
            raw = json.loads(
                (staging / "_generation" / RAW_REALIZATION_NAME).read_text()
            )
            self.assertNotIn(
                "emails[].body",
                dumped["targets"]["service:o1:email_client"]["collections"]
                ["emails"]["records"][0]["values"],
            )
            self.assertEqual(raw, dumped)
            validation = json.loads(
                (staging / "_generation" / "validation.json").read_text()
            )
            self.assertTrue(validation["issues"])
            self.assertFalse(
                (staging / "_generation" / "bundle_manifest.json").exists()
            )
            self.assertEqual(marker.read_text(), original)

    def test_compiled_bundle_keeps_raw_realization(self):
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td)
            staging = begin_stage4_attempt(
                out_root,
                "T0003-g-b-h",
                attempt_id="keep-raw",
                realization=self.realization,
            )
            original_raw = (
                staging / "_generation" / RAW_REALIZATION_NAME
            ).read_text()
            persist_stage4_text_audit(
                staging, "world_generation/prompt-1.txt", "full prompt"
            )
            compiled, report = self._materialize(
                out_root,
                "keep-raw",
                promote=False,
                seal=False,
            )
            self.assertTrue(report["pass"])
            self.assertEqual(compiled, staging)
            self.assertEqual(
                (compiled / "_generation" / RAW_REALIZATION_NAME).read_text(),
                original_raw,
            )
            self.assertEqual(
                (
                    compiled / "_generation" / "audits"
                    / "world_generation" / "prompt-1.txt"
                ).read_text(),
                "full prompt",
            )
            self.assertFalse((out_root / "T0003-g-b-h").exists())

    def test_stage5_rejects_absent_or_stale_stage4_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td)
            bundle, report = self._materialize(
                out_root,
                "ok",
                inject_passing_gates=True,
            )
            self.assertTrue(report["pass"])
            manifest = require_stage4_receipt(bundle)
            self.assertEqual(manifest["_gate"]["stage"], "4")
            (bundle / "_generation" / "bundle_manifest.json").unlink()
            with self.assertRaises(SystemExit):
                require_stage4_receipt(bundle)
            seal_stage4_bundle(
                bundle,
                inputs={"stale": True},
                models={},
                passed=False,
            )
            with self.assertRaises(SystemExit):
                require_stage4_receipt(bundle)

    def test_single_stage4_receipt_consumer(self):
        from authorization_eva_sandbox.task_generation.run_v3 import parse_args
        from unittest.mock import patch
        with patch("sys.argv", ["run_v3", "--seed-id", "S0003", "--from", "4", "--to", "4"]):
            args = parse_args()
        self.assertFalse(hasattr(args, "stage4_rounds"))
        source = (
            Path(__file__).resolve().parents[1] / "task_generation" / "run_v3.py"
        ).read_text()
        self.assertNotIn("--boot-check", source)
        self.assertNotIn("--compile-bindings", source)
        self.assertIn("One Stage 4 invocation", source)

    def test_stage4_review_and_repair_use_text_caller(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "task_generation" / "stages" / "materialize" / "stage4.py"
        ).read_text()
        critic = source.split("def _critic(", 1)[1].split("def _compile_and_boot(", 1)[0]
        repair = source.split("def _repair_model(", 1)[1].split(
            "binding_report = prepare_world_realization", 1
        )[0]
        classify = source.split("def _classify(", 1)[1].split("def _repair_model(", 1)[0]
        self.assertIn("complete_text(", critic)
        self.assertNotIn("_call_stage2_step(", critic)
        self.assertIn("complete_text(", repair)
        self.assertNotIn("_call_stage2_step(", repair)
        self.assertIn("_call_stage2_step(", classify)
        prepare_at = source.find("binding_report = prepare_world_realization")
        format_at = source.find("repair_format_mismatches(")
        validate_at = source.find("issues = validate_world_realization")
        self.assertTrue(0 < prepare_at < format_at < validate_at)
        format_chunk = source[format_at:validate_at]
        self.assertIn("repair_call=_repair_model", format_chunk)
        self.assertNotIn("_critic", format_chunk)

    def test_world_generation_uses_strict_text_parse_and_bounded_repair(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "task_generation" / "stages" / "materialize" / "stage4.py"
        ).read_text()
        generation = source.split(
            "prompt = build_world_realization_prompt", 1
        )[1].split("def _classify(", 1)[0]
        self.assertIn("complete_text(", generation)
        self.assertIn("parse_world_realization_text(", generation)
        self.assertIn("classify_world_generation_failure(", generation)
        self.assertIn("rewind_incomplete_world_json(", generation)
        self.assertIn("build_world_realization_continuation_prompt(", generation)
        self.assertIn("build_world_realization_repair_prompt(", generation)
        self.assertIn("GENERATION_FIRST_MAX_TOKENS", generation)
        compile_loop = source.split("compiled = _compile(realization)", 1)[1]
        self.assertIn("findings_for_compile_issues(", compile_loop)
        self.assertIn("repair_timeplan_findings(", compile_loop)
        self.assertLess(
            compile_loop.find("findings_for_compile_issues("),
            compile_loop.find("Stage 4 compilation failed after one repair"),
        )
        self.assertNotIn("(16000, 24000)", generation)
        self.assertNotIn("raw = _call_stage2_step(", generation)

    def test_grounding_is_sparse_and_does_not_annotate_every_binding(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "task_generation" / "stages" / "materialize" / "stage4.py"
        ).read_text()
        prompt = source.split("STAGE4_GROUNDING_PROMPT = ", 1)[1].split(
            "STAGE4_GROUNDING_CRITIC_PROMPT = ", 1
        )[0]
        compiler = source.split("def compile_grounding(", 1)[1].split(
            "def _validate_against_schema(", 1
        )[0]
        self.assertIn("SPARSE symbol table", prompt)
        self.assertIn("empty references list", prompt)
        self.assertNotIn('"realizations":', prompt)
        self.assertNotIn("no realization_note", compiler)
        self.assertNotIn('b["realization_note"]', compiler)
        self.assertNotIn('"realizations":', compiler)
        self.assertNotIn("instruction contains deictic references", compiler)

    def test_omitting_coverage_does_not_fail_structural_compile(self):
        self.assertNotIn("coverage", self.realization)
        with tempfile.TemporaryDirectory() as td:
            staging, report = self._materialize(
                Path(td),
                "no-coverage",
                promote=False,
                seal=False,
            )
            self.assertTrue(report["pass"])
            self.assertFalse(
                any("coverage" in issue.lower() for issue in report["issues"])
            )
            self.assertFalse(
                (staging / "_generation" / "coverage_issues.json").exists()
            )
            manifest = json.loads(
                (staging / "_generation" / "manifest.json").read_text()
            )
            m2 = next(
                item for item in manifest["materials"] if item["material_id"] == "m2"
            )
            self.assertEqual(m2["status"], "pending_content")

    def test_require_stage4_receipt_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            bundle, report = self._materialize(
                Path(td),
                "tamper",
                inject_passing_gates=True,
            )
            self.assertTrue(report["pass"])
            require_stage4_receipt(bundle)
            config = bundle / "task_config.json"
            config.write_text(config.read_text() + "\n")
            with self.assertRaises(SystemExit) as raised:
                require_stage4_receipt(bundle)
            self.assertIn("hashes do not match", str(raised.exception))

    def test_registry_drift_stops_live_stage4(self):
        blueprint = _load_live("V0010-l-b-h", "blueprint")
        broken = json.loads(json.dumps(blueprint))
        broken["_gate"]["inputs"]["registry"] = "0" * 64
        with self.assertRaises(FillEnvelopeError):
            require_stage3_registry_match(broken)
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FillEnvelopeError):
                materialize_world_offline(
                    out_root=Path(td),
                    seed={"seed_id": "S0010"},
                    stem="V0010-l-b-h",
                    policy_skeleton=_load_live("V0010-l-b-h", "policy_skeleton"),
                    script=_load_live("V0010-l-b-h", "script"),
                    surface=_load_live("V0010-l-b-h", "surface"),
                    blueprint=broken,
                    realization=self.realization,
                    registry=self.registry,
                    attempt_id="drift",
                    promote=False,
                )

    def test_sealed_task_id_survives_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td)
            staging, report = self._materialize(
                out_root,
                "attempt-name",
                promote=False,
                seal=True,
                inject_passing_gates=True,
            )
            self.assertTrue(report["pass"])
            self.assertEqual(staging.name, "attempt-name")
            sealed = json.loads(
                (staging / "_generation" / "bundle_manifest.json").read_text()
            )
            self.assertEqual(sealed["task_id"], "T0003-g-b-h")
            self.assertNotEqual(sealed["task_id"], staging.name)
            with self.assertRaises(SystemExit) as raised:
                require_stage4_receipt(staging)
            self.assertIn("does not match bundle", str(raised.exception))
            from authorization_eva_sandbox.task_generation.shared.world_stage4 import (
                promote_stage4_bundle,
                canonical_dir,
            )
            canonical = canonical_dir(out_root, "T0003-g-b-h")
            promote_stage4_bundle(staging, canonical)
            manifest = require_stage4_receipt(canonical)
            self.assertEqual(manifest["task_id"], canonical.name)
            self.assertEqual(manifest["task_id"], "T0003-g-b-h")

    def test_receipt_covers_generation_artifacts_without_self_hash(self):
        with tempfile.TemporaryDirectory() as td:
            bundle, report = self._materialize(
                Path(td),
                "receipt-inputs",
                inject_passing_gates=True,
            )
            self.assertTrue(report["pass"])
            manifest = require_stage4_receipt(bundle)
            files = manifest["files"]
            self.assertNotIn("_generation/bundle_manifest.json", files)
            for name in (
                "world_review.json",
                "validation.json",
                "compiler_report.json",
                "bindings.json",
                "binding_report.json",
                "leak_report.json",
                "boot_report.json",
            ):
                self.assertIn(f"_generation/{name}", files)
                self.assertIn(name, GENERATION_INPUTS)
            self.assertNotIn("_generation/coverage_issues.json", files)
            gate_inputs = manifest["_gate"]["inputs"]
            for key in (
                "compiler_report",
                "bindings",
                "binding_report",
                "world_review",
                "validation",
                "leak_report",
                "boot_report",
            ):
                self.assertIn(key, gate_inputs)
            self.assertNotIn("coverage_issues", gate_inputs)
            review = bundle / "_generation" / "world_review.json"
            review.write_text(review.read_text() + "\n")
            with self.assertRaises(SystemExit) as raised:
                require_stage4_receipt(bundle)
            self.assertIn("hashes do not match", str(raised.exception))
            current = build_bundle_manifest(bundle)
            self.assertNotIn(
                "_generation/bundle_manifest.json", current["files"]
            )

    def test_compile_does_not_manufacture_review_or_leak(self):
        with tempfile.TemporaryDirectory() as td:
            staging, report = self._materialize(
                Path(td),
                "compile-only",
                promote=False,
                seal=False,
            )
            self.assertTrue(report["pass"])
            generation = staging / "_generation"
            self.assertFalse((generation / "world_review.json").exists())
            self.assertFalse((generation / "leak_report.json").exists())
            self.assertFalse((generation / "boot_report.json").exists())
            self.assertFalse((generation / "bundle_manifest.json").exists())
            self.assertTrue((generation / "binding_report.json").is_file())
            self.assertTrue((generation / "validation.json").is_file())

    def test_seal_without_passing_gates_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            staging, report = self._materialize(
                Path(td),
                "no-gates",
                promote=False,
                seal=False,
            )
            self.assertTrue(report["pass"])
            with self.assertRaises(SystemExit) as raised:
                seal_stage4_bundle(
                    staging,
                    inputs={},
                    models={"generator": "offline", "critic": "offline"},
                    passed=True,
                )
            self.assertIn("cannot seal", str(raised.exception))
            self.assertFalse(
                (staging / "_generation" / "bundle_manifest.json").exists()
            )

    def _run_loop(self, staging, gates, repair_text="replaced body"):
        repair_calls = []
        recompile_calls = []

        def _repair(_prompt: str) -> str:
            repair_calls.append(_prompt)
            return _replacement(repair_text)

        def _recompile(patched):
            recompile_calls.append(patched)
            return {"realization": patched, "pass": True}

        with patch(
            "authorization_eva_sandbox.task_generation.shared.world_stage4.run_stage4_semantic_gates",
            side_effect=gates,
        ):
            realization, audit = run_stage4_repair_loop(
                staging=staging,
                envelope=self.envelope,
                realization=self.realization,
                registry=self.registry,
                policy_skeleton=self.policy_skeleton,
                review_call=lambda _prompt: "PASS",
                leak_call=lambda _prompt: "PASS",
                repair_call=_repair,
                reference_time="2026-06-06T10:11:00Z",
                recompile=_recompile,
            )
        return realization, audit, repair_calls, recompile_calls

    def test_one_replacement_then_rerun_passes(self):
        finding = self._body_finding()
        gates = [
            (
                {
                    "pass": False,
                    "problems": [],
                    "targets": [{"findings": [finding]}],
                },
                {"pass": True, "problems": [], "findings": []},
            ),
            (
                {
                    "pass": True,
                    "problems": [],
                    "targets": [],
                    "repair": {"applied": True},
                },
                {"pass": True, "problems": [], "findings": []},
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "attempt"
            staging.mkdir()
            realization, audit, repair_calls, recompile_calls = self._run_loop(
                staging, gates
            )
        self.assertTrue(audit["pass"])
        self.assertFalse(audit["stopped"])
        self.assertEqual(len(repair_calls), 1)
        self.assertEqual(len(recompile_calls), 1)
        self.assertTrue(audit["repair"]["applied"])
        self.assertNotIn("REPLACEMENT", json.dumps(realization))
        body = realization["targets"]["service:o1:email_client"]["collections"][
            "emails"
        ]["records"][0]["values"]["emails[].body"]
        self.assertEqual(body, "replaced body")

    def test_non_repairable_finding_stops_without_replacement(self):
        finding = self._body_finding(repairable=False)
        gates = [
            (
                {
                    "pass": False,
                    "problems": [],
                    "targets": [{"findings": [finding]}],
                },
                {"pass": True, "problems": [], "findings": []},
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "attempt"
            staging.mkdir()
            realization, audit, repair_calls, recompile_calls = self._run_loop(
                staging, gates
            )
        self.assertFalse(audit["pass"])
        self.assertTrue(audit["stopped"])
        self.assertFalse(repair_calls)
        self.assertFalse(recompile_calls)
        self.assertFalse(audit["repair"]["applied"])
        self.assertEqual(realization, self.realization)
        self.assertTrue(
            any("non-repairable" in item for item in audit["problems"])
        )

    def test_second_failure_after_replacement_stops(self):
        finding = self._body_finding()
        gates = [
            (
                {
                    "pass": False,
                    "problems": [],
                    "targets": [{"findings": [finding]}],
                },
                {"pass": True, "problems": [], "findings": []},
            ),
            (
                {
                    "pass": False,
                    "problems": [],
                    "targets": [{"findings": [finding]}],
                },
                {"pass": True, "problems": [], "findings": []},
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "attempt"
            staging.mkdir()
            _, audit, repair_calls, recompile_calls = self._run_loop(
                staging, gates
            )
        self.assertFalse(audit["pass"])
        self.assertTrue(audit["stopped"])
        self.assertEqual(len(repair_calls), 1)
        self.assertEqual(len(recompile_calls), 1)
        self.assertTrue(audit["repair"]["applied"])
        self.assertTrue(
            any("second semantic pass" in item for item in audit["problems"])
        )

    def test_failed_semantic_attempt_is_not_promoted(self):
        finding = self._body_finding(repairable=False)
        with tempfile.TemporaryDirectory() as td:
            out_root = Path(td)
            canonical, report = self._materialize(
                out_root,
                "good",
                inject_passing_gates=True,
            )
            self.assertTrue(report["pass"])
            original = (canonical / "task_config.json").read_text()
            staging, compiled = self._materialize(
                out_root,
                "semantic-fail",
                promote=False,
                seal=False,
            )
            self.assertTrue(compiled["pass"])
            _, audit, _, _ = self._run_loop(
                staging,
                [
                    (
                        {
                            "pass": False,
                            "problems": [],
                            "targets": [{"findings": [finding]}],
                        },
                        {"pass": True, "problems": [], "findings": []},
                    ),
                ],
            )
            self.assertTrue(audit["stopped"])
            self.assertFalse(
                (staging / "_generation" / "bundle_manifest.json").exists()
            )
            self.assertEqual((canonical / "task_config.json").read_text(), original)
            self.assertTrue(
                (attempt_dir(out_root, "T0003-g-b-h", "semantic-fail")
                 / "task_config.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
