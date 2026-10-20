from __future__ import annotations

import copy
import unittest

from authorization_eva_sandbox.task_generation.shared.joint_material_graph import (
    apply_joint_graph_patches,
    canonicalize_joint_graph_field_paths,
    canonicalize_joint_graph_structure,
    deterministic_conflict_surgery,
    validate_joint_material_graph,
)


class JointMaterialGraphTests(unittest.TestCase):
    def setUp(self):
        self.contracts = {
            "m2": {
                "scalar_model_fields": ["summary.recipients"],
                "collection_model_fields": {},
            },
            "m3": {
                "scalar_model_fields": [],
                "collection_model_fields": {
                    "events": ["events[].attendees"],
                },
            },
        }
        self.graph = {
            "materials": {
                "m2": {
                    "scalar_values": {
                        "summary.recipients": ["a@example.com"],
                    },
                    "collections": {},
                },
                "m3": {
                    "scalar_values": {},
                    "collections": {
                        "events": {
                            "mode": "exhaustive",
                            "count": 1,
                            "records": [{
                                "values": {
                                    "events[].attendees": ["a@example.com"],
                                },
                            }],
                        },
                    },
                },
            },
            "relations": [{
                "left": "/materials/m2/scalar_values/summary.recipients",
                "op": "equal",
                "right": (
                    "/materials/m3/collections/events/records/0/values/"
                    "events[].attendees"
                ),
                "reason": "Same recipients.",
            }],
        }

    def test_relation_json_pointers_are_executed(self):
        self.assertEqual(
            validate_joint_material_graph(
                self.graph, contracts=self.contracts
            ),
            [],
        )

    def test_unresolvable_relation_pointer_fails(self):
        graph = copy.deepcopy(self.graph)
        graph["relations"][0]["right"] = (
            "/materials/m3/events/events/records/0"
        )
        issues = validate_joint_material_graph(
            graph, contracts=self.contracts
        )
        self.assertTrue(any("does not resolve" in issue for issue in issues))

    def test_false_relation_fails(self):
        graph = copy.deepcopy(self.graph)
        graph["materials"]["m2"]["scalar_values"][
            "summary.recipients"
        ] = ["other@example.com"]
        issues = validate_joint_material_graph(
            graph, contracts=self.contracts
        )
        self.assertTrue(any("does not hold" in issue for issue in issues))

    def test_subset_normalizes_comma_separated_collection(self):
        graph = copy.deepcopy(self.graph)
        graph["materials"]["m2"]["scalar_values"][
            "summary.recipients"
        ] = "a@example.com,b@example.com"
        graph["relations"][0]["op"] = "subset"
        graph["relations"][0]["left"] = (
            "/materials/m3/collections/events/records/0/values/"
            "events[].attendees"
        )
        graph["relations"][0]["right"] = (
            "/materials/m2/scalar_values/summary.recipients"
        )
        self.assertEqual(
            validate_joint_material_graph(graph, contracts=self.contracts),
            [],
        )

    def test_targeted_patch_preserves_unrelated_values(self):
        graph = copy.deepcopy(self.graph)
        graph["materials"]["m3"]["collections"]["events"]["count"] = 2
        patched, issues = apply_joint_graph_patches(graph, [
            {
                "op": "replace",
                "path": "/materials/m3/collections/events/count",
                "value": 1,
            },
            {"op": "remove", "path": "/relations/0"},
        ])
        self.assertEqual(issues, [])
        self.assertEqual(
            patched["materials"]["m3"]["collections"]["events"]["count"], 1
        )
        self.assertEqual(patched["relations"], [])
        self.assertEqual(
            patched["materials"]["m3"]["collections"]["events"]["records"],
            self.graph["materials"]["m3"]["collections"]["events"]["records"],
        )

    def test_exhaustive_count_is_compiler_owned(self):
        graph = copy.deepcopy(self.graph)
        graph["materials"]["m3"]["collections"]["events"]["count"] = 99
        changes = canonicalize_joint_graph_structure(graph)
        self.assertEqual(
            graph["materials"]["m3"]["collections"]["events"]["count"], 1
        )
        self.assertTrue(changes)

    def test_multiple_array_removes_are_descending_and_transactional(self):
        graph = copy.deepcopy(self.graph)
        graph["relations"].append(copy.deepcopy(graph["relations"][0]))
        patched, issues = apply_joint_graph_patches(graph, [
            {"op": "remove", "path": "/relations/0"},
            {"op": "remove", "path": "/relations/1"},
        ])
        self.assertEqual(issues, [])
        self.assertEqual(patched["relations"], [])

        unchanged, issues = apply_joint_graph_patches(graph, [
            {"op": "remove", "path": "/relations/9"},
        ])
        self.assertTrue(issues)
        self.assertEqual(unchanged, graph)

    def test_structured_conflict_is_unified_without_regeneration(self):
        graph = copy.deepcopy(self.graph)
        graph["materials"]["m2"]["scalar_values"][
            "summary.recipients"
        ] = "a@example.com,b@example.com"
        conflict = {
            "conflict_id": "recipients",
            "kind": "set_mismatch",
            "occurrences": [
                {
                    "path": (
                        "/materials/m2/scalar_values/summary.recipients"
                    ),
                    "value": "a@example.com,b@example.com",
                },
                {
                    "path": (
                        "/materials/m3/collections/events/records/0/values/"
                        "events[].attendees"
                    ),
                    "value": ["a@example.com"],
                },
            ],
        }
        repaired, unresolved, changes, issues = (
            deterministic_conflict_surgery(
                graph, [conflict], story=""
            )
        )
        self.assertEqual(issues, [])
        self.assertEqual(unresolved, [])
        self.assertTrue(changes)
        self.assertEqual(
            repaired["materials"]["m3"]["collections"]["events"]["records"][
                0
            ]["values"]["events[].attendees"],
            ["a@example.com", "b@example.com"],
        )

    def test_prose_conflict_is_left_for_constrained_surgery(self):
        conflict = {
            "conflict_id": "unsupported-name",
            "kind": "unsupported_prose_fact",
            "occurrences": [{
                "path": "/materials/m2/scalar_values/summary.recipients",
                "value": ["a@example.com"],
            }],
        }
        repaired, unresolved, changes, issues = (
            deterministic_conflict_surgery(
                self.graph, [conflict], story=""
            )
        )
        self.assertEqual(repaired, self.graph)
        self.assertEqual(unresolved, [conflict])
        self.assertEqual(changes, [])
        self.assertEqual(issues, [])

    def test_relative_record_keys_are_compiler_normalized(self):
        graph = copy.deepcopy(self.graph)
        record = graph["materials"]["m3"]["collections"]["events"]["records"][0]
        record["values"] = {
            "attendees": [{
                "name": "A",
                "email": "a@example.com",
                "response_status": "accepted",
            }],
            "snippet": "derived by service",
        }
        changes = canonicalize_joint_graph_field_paths(
            graph, contracts=self.contracts
        )
        self.assertEqual(
            record["values"],
            {"events[].attendees": [{
                "name": "A",
                "email": "a@example.com",
                "response_status": "accepted",
            }]},
        )
        self.assertTrue(any("dropped unknown field" in item for item in changes))


if __name__ == "__main__":
    unittest.main()
