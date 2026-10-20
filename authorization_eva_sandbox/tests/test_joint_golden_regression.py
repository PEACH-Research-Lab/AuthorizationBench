from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from authorization_eva_sandbox.services.registry import ServiceRegistry
from authorization_eva_sandbox.task_generation.shared.joint_material_graph import (
    build_joint_contracts,
    canonicalize_joint_graph_field_paths,
    canonicalize_joint_graph_structure,
    compile_joint_material_graph,
    validate_joint_material_graph,
)
from authorization_eva_sandbox.task_generation.stages.assembly.stage3 import (
    resolve_blueprint_slots,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v4"


class JointGoldenRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load()

    def _compile(self, stem: str):
        script = json.loads(
            (FIXTURES / f"{stem}.script.json").read_text()
        )["step2_script"]
        blueprint = json.loads(
            (FIXTURES / f"{stem}.blueprint.json").read_text()
        )["assembly_blueprint"]
        blueprint = resolve_blueprint_slots(
            blueprint, script, self.registry
        )
        contracts = build_joint_contracts(
            script=script, blueprint=blueprint, registry=self.registry
        )
        graph = copy.deepcopy(json.loads(
            (FIXTURES / f"{stem}.joint_graph.json").read_text()
        )["graph"])
        canonicalize_joint_graph_field_paths(graph, contracts=contracts)
        canonicalize_joint_graph_structure(graph)
        self.assertEqual(
            validate_joint_material_graph(graph, contracts=contracts), []
        )
        fixtures, runtime, reports, issues = compile_joint_material_graph(
            graph,
            contracts=contracts,
            registry=self.registry,
            principal={
                "user_name": "Priya Sharma",
                "user_email": "priya@example.com",
            },
            reference_time="2026-06-06T10:11:00",
        )
        self.assertEqual(issues, [])
        return graph, fixtures, runtime, reports

    def test_s0010_cached_joint_values_compile_offline(self):
        graph, fixtures, runtime, reports = self._compile("V0010-l-b-h")
        self.assertEqual(set(fixtures), {"calendar", "email_client", "zoom"})
        self.assertTrue(runtime["prior_memory_md"])
        self.assertTrue(runtime["tree_specs"])
        self.assertEqual(set(reports), set(graph["materials"]))
        rendered = json.dumps(fixtures, ensure_ascii=False)
        self.assertIsNone(re.search(r"\bm\d+-", rendered))

    def test_s0003_cached_joint_values_preserve_buried_record(self):
        graph, fixtures, _, _ = self._compile("V0003-g-b-h")
        script = json.loads(
            (FIXTURES / "V0003-g-b-h.script.json").read_text()
        )["step2_script"]
        email_material = next(
            material["id"] for material in script["materials"]
            if material["kind"] == "email_collection"
        )
        records = graph["materials"][email_material]["collections"]["emails"]["records"]
        self.assertEqual(len(records), 30)
        decisive = [
            index for index, record in enumerate(records)
            if all(
                token in record["values"]["emails[].body"].lower()
                for token in ("approved", "pending", "case")
            )
        ]
        self.assertEqual(len(decisive), 1)
        self.assertLess(decisive[0], len(records) / 2)
        self.assertEqual(len(fixtures["email_client"]["emails"]), 30)
        corpus = json.dumps(fixtures["email_client"], ensure_ascii=False).lower()
        self.assertNotIn("30 emails", corpus)
        self.assertNotIn("result set too large", corpus)


if __name__ == "__main__":
    unittest.main()
