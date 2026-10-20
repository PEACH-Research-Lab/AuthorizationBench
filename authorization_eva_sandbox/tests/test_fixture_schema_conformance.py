"""FIXTURE_SCHEMA <-> load_data conformance (the anti-drift binding).

FIXTURE_SCHEMA (declared contract, prompt/D-gate input) and load_data
(implementation) encode overlapping facts. This test binds them so drift
cannot hide:

1. Every top-level schema key must appear as a string literal in the
   service's source (a renamed/removed fixture key fails immediately).
2. A synthetic fixture generated from the schema must validate against the
   schema itself (schema is internally consistent).
3. The service must really BOOT with that synthetic fixture (load_data
   consumes it without crashing) and get_state() must return a dict.
4. Synthesized services: the spec's entity fields must equal the generated
   FIXTURE_SCHEMA collections (spec -> codegen chain is intact).

No LLM calls; runs on every converted template that declares a schema.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SANDBOX_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SANDBOX_ROOT.parent
for p in (str(SANDBOX_ROOT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from services._base import (  # noqa: E402
    fixture_env_var,
    is_valid_leaf_type,
    synthetic_fixture,
    validate_fixture,
)
from services.registry import ServiceRegistry, derive_fixture_schema  # noqa: E402


def _schema_types_valid(spec, where, problems):
    if isinstance(spec, str):
        if not is_valid_leaf_type(spec):
            problems.append(f"{where}: unknown type {spec!r}")
    elif isinstance(spec, dict):
        for k, v in spec.items():
            _schema_types_valid(v, f"{where}.{k}", problems)
    elif isinstance(spec, list):
        if len(spec) != 1 or not isinstance(spec[0], dict):
            problems.append(f"{where}: collection spec must be [record-dict]")
        else:
            _schema_types_valid(spec[0], f"{where}[]", problems)
    else:
        problems.append(f"{where}: invalid spec node {spec!r}")


class FixtureSchemaConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = ServiceRegistry.load(derive_live=False)
        cls.templates = [
            t for t in cls.registry.converted_templates()
            if derive_fixture_schema(t) is not None
        ]

    def test_at_least_core_templates_declare_schema(self):
        declared = set(self.templates)
        for must in ("email_client", "zoom", "shopping_app_instacart",
                     "comparison_shopping", "dev_forum", "slack_workspace",
                     "quickgist_ai", "calendar", "wechat_app"):
            self.assertIn(must, declared,
                          f"{must} must declare FIXTURE_SCHEMA (Stage 4 depends on it)")

    def test_user_messaging_is_config_driven(self):
        # Deliberate asymmetry (see services/user_messaging/service.py):
        # runtime infrastructure reads TASK_CONFIG, never a fixture file.
        self.assertIsNone(derive_fixture_schema("user_messaging"),
                          "user_messaging must NOT declare FIXTURE_SCHEMA — "
                          "it is config-driven runtime infrastructure")

    def test_schema_type_vocabulary(self):
        for t in self.templates:
            problems: list[str] = []
            _schema_types_valid(derive_fixture_schema(t), t, problems)
            self.assertEqual(problems, [])

    def test_schema_keys_appear_in_source(self):
        for t in self.templates:
            source = (SANDBOX_ROOT / "services" / t / "service.py").read_text()
            missing = [k for k in derive_fixture_schema(t)
                       if f'"{k}"' not in source and f"'{k}'" not in source]
            self.assertEqual(
                missing, [],
                f"{t}: schema keys {missing} never referenced in service.py "
                "— schema drifted from load_data")

    def test_boot_with_synthetic_fixture(self):
        import importlib

        for t in self.templates:
            schema = derive_fixture_schema(t)
            fixture = synthetic_fixture(schema)
            self.assertEqual(validate_fixture(fixture, schema), [],
                             f"{t}: synthetic fixture fails its own schema")
            service_id = f"conftest_{t}"
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as fh:
                json.dump(fixture, fh)
                path = fh.name
            os.environ[fixture_env_var(service_id)] = path
            try:
                module = importlib.import_module(f"services.{t}.service")
                svc = module.create_service(service_id)
                # Runtime contract: multi_server (re)loads fixtures after
                # mounting; some create_service impls also call it — calling
                # twice must be safe.
                svc.load_data()
                state = svc.get_state()
                self.assertIsInstance(state, dict, f"{t}: get_state not a dict")
            finally:
                os.environ.pop(fixture_env_var(service_id), None)
                Path(path).unlink(missing_ok=True)

    def test_state_full_exposure(self):
        """Full-exposure convention (owner decision 2026-08-18): every
        fixture-loaded business fact must be evaluator-visible via
        get_state — /state is infra-only (never an agent tool), and judges
        bind evidence to it.

        Transform tolerance: services may reinterpret enum-ish inputs (zoom
        turns an invalid desktop.state into a real state), so per top-level
        schema key we require the key itself to surface in the state JSON,
        or >=50% of its seeded string leaves to remain visible."""
        import importlib

        def _string_leaves(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for v in node.values():
                    yield from _string_leaves(v)
            elif isinstance(node, list):
                for v in node:
                    yield from _string_leaves(v)

        for t in self.templates:
            schema = derive_fixture_schema(t)
            fixture = synthetic_fixture(schema)
            service_id = f"exposure_{t}"
            with tempfile.NamedTemporaryFile("w", suffix=".json",
                                             delete=False) as fh:
                json.dump(fixture, fh)
                path = fh.name
            os.environ[fixture_env_var(service_id)] = path
            try:
                module = importlib.import_module(f"services.{t}.service")
                svc = module.create_service(service_id)
                svc.load_data()
                dump = json.dumps(svc.get_state(), ensure_ascii=False)
            finally:
                os.environ.pop(fixture_env_var(service_id), None)
                Path(path).unlink(missing_ok=True)
            for key, spec in schema.items():
                if f'"{key}"' in dump:
                    continue
                leaves = [s for s in _string_leaves(
                    {key: synthetic_fixture({key: spec})[key]})
                    if s.startswith("seed-")]
                if not leaves:
                    continue
                visible = sum(1 for s in leaves if s in dump)
                self.assertGreaterEqual(
                    visible / len(leaves), 0.5,
                    f"{t}: fixture key {key!r} not exposed in get_state "
                    f"({visible}/{len(leaves)} leaves visible) — violates "
                    "the full-exposure convention")

    def test_synthesized_services_schema_matches_spec(self):
        spec_dir = SANDBOX_ROOT / "services" / "_synthesis"
        if not spec_dir.is_dir():
            return
        for spec_path in spec_dir.glob("*.spec.json"):
            spec = json.loads(spec_path.read_text())
            template = spec.get("template", "")
            if template not in self.templates:
                continue  # drafted but never built
            schema = derive_fixture_schema(template)
            for e in spec.get("entities", []):
                self.assertIn(e["plural"], schema,
                              f"{template}: collection {e['plural']} missing")
                self.assertEqual(schema[e["plural"]][0], e["fields"],
                                 f"{template}: schema drifted from spec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
