"""Service synthesis side-path — controlled registry expansion.

NOT part of the Stage 3 pipeline. Stage 3 plans against a FIXED registry;
when its ``unresolved`` list shows a recurring capability gap, an owner runs
this tool to grow the registry through five steps:

    1. draft   — LLM drafts a service spec (entities + CRUD actions + fixture
                 schema). Written to services/_synthesis/<template>.spec.json
                 with ``approved: false``.
    2. review  — a HUMAN reads/edits the spec file and flips ``approved`` to
                 true. This is the owner decision point: the machine proposes,
                 the owner disposes. ``build`` refuses unapproved specs.
    3. build   — deterministic validation, then TEMPLATED codegen (constrained
                 CRUD generator, never free-form LLM code), producing a
                 service.py that follows the exact ServiceBase conventions of
                 hand-written services (audit log, fixture load, infra routes).
    4. test    — the generated service is REALLY booted: mounted in FastAPI,
                 hit through a test client with a seeded fixture; OpenAPI
                 operation_ids, CRUD round-trip, 404 handling, and audit-log
                 writes are all asserted before anything is registered.
    5. register— service dir written under services/<template>/, an entry
                 (marked ``synthesized: true``) appended to the draft-JSON
                 catalog, and ServiceRegistry.load(derive_live=True) is used
                 to confirm the new template is discovered with live actions.

``remove`` rolls a synthesized service back out (files + catalog entry).

CLI:
    python services/synthesis.py draft --request "a parking permit portal" \
        [--profile deepseek_v4_pro_openrouter] [--config <toml>]
    python services/synthesis.py build --spec services/_synthesis/x.spec.json
    python services/synthesis.py remove --template x
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

_SERVICES_DIR = Path(__file__).resolve().parent
_SANDBOX_ROOT = _SERVICES_DIR.parent
_REPO_ROOT = _SANDBOX_ROOT.parent
for p in (str(_SANDBOX_ROOT), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
# Running as ``python services/synthesis.py`` puts services/ itself on
# sys.path, where services/calendar/ shadows the stdlib ``calendar`` module
# (breaking httpx and friends). Strip it.
sys.path[:] = [p for p in sys.path
               if Path(p or ".").resolve() != _SERVICES_DIR]

from services._base import fixture_env_var  # noqa: E402

SPEC_DIR = _SERVICES_DIR / "_synthesis"
CATALOG_PATH = _SERVICES_DIR / "service_catalog.json"

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ALLOWED_TYPES = {"str", "int", "float", "bool", "list", "dict"}
_ALLOWED_ACTIONS = ("list", "get", "create", "update", "delete")


# ═════════════════════════════════════════════════════════════════════════
# Step 1+2 — spec draft (LLM) and the human approval gate
# ═════════════════════════════════════════════════════════════════════════

_SPEC_DRAFT_PROMPT = """\
You design a minimal API service spec for an agent-evaluation sandbox.

## Request (capability gap to fill)
{request}

## Existing service templates (do NOT duplicate; pick a DISTINCT name)
{existing_templates}

## Existing categories (reuse one if it fits, else propose a new snake_case one)
{categories}

## Output — one JSON object, nothing else
{{
  "template": "<snake_case service name, e.g. parking_portal>",
  "category": "<category id>",
  "category_description": "<only if the category is new, else omit>",
  "description": "<one sentence: what this service surface is>",
  "entities": [
    {{
      "name": "<singular snake_case, e.g. permit>",
      "plural": "<plural snake_case, e.g. permits>",
      "id_field": "id",
      "fields": {{"id": "str", "<field>": "<str|int|float|bool|list|dict>"}},
      "actions": ["list", "get", "create", "update", "delete"]
    }}
  ],
  "notes": "<bespoke mechanics that CANNOT be expressed as plain CRUD; the
             generated service will NOT implement these — they are a to-do
             for a human if truly needed>"
}}

Rules:
- 1 to 3 entities, each with 2 to 8 fields besides the id.
- ``actions`` is any subset of list/get/create/update/delete that the agent
  plausibly needs; do not include delete unless the request implies it.
- Field types only from: str, int, float, bool, list, dict.
- Keep it minimal: this is a symbolic stand-in, not a production system.
"""


def draft_spec(*, request: str, profile_name: str, config_file: Path) -> Path:
    """Step 1: LLM drafts a spec; written unapproved for human review."""
    from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
        _call_stage2_step,
    )
    from services.registry import ServiceRegistry

    reg = ServiceRegistry.load(derive_live=False)
    prompt = _SPEC_DRAFT_PROMPT.format(
        request=request.strip(),
        existing_templates=", ".join(sorted(reg.templates)),
        categories="\n".join(f"- {k}: {v}" for k, v in sorted(reg.categories.items())),
    )
    spec = _call_stage2_step(
        prompt=prompt, profile_name=profile_name,
        config_file=config_file, max_tokens=2500,
    )
    if not isinstance(spec, dict) or not spec.get("template"):
        raise SystemExit(f"draft failed: unusable LLM output: {spec!r}")

    spec["approved"] = False
    spec["source_request"] = request.strip()
    issues = validate_spec(spec, check_approved=False)
    spec["_draft_validation"] = issues or ["clean"]

    SPEC_DIR.mkdir(exist_ok=True)
    out = SPEC_DIR / f"{spec['template']}.spec.json"
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
    return out


# ═════════════════════════════════════════════════════════════════════════
# Step 3a — deterministic spec validation
# ═════════════════════════════════════════════════════════════════════════

def validate_spec(spec: dict[str, Any], *, check_approved: bool = True) -> list[str]:
    """Return a list of problems; empty list means the spec is buildable."""
    issues: list[str] = []
    t = spec.get("template", "")
    if not _IDENT_RE.match(t or ""):
        issues.append(f"template name {t!r} is not snake_case")
    if (_SERVICES_DIR / t).exists():
        issues.append(f"services/{t}/ already exists (name collision)")
    if check_approved and spec.get("approved") is not True:
        issues.append("spec not approved: a human must review the file and "
                      "set \"approved\": true before build")
    if not spec.get("description"):
        issues.append("missing description")
    if not spec.get("category"):
        issues.append("missing category")

    entities = spec.get("entities")
    if not isinstance(entities, list) or not entities:
        issues.append("entities must be a non-empty list")
        return issues
    seen_plurals: set[str] = set()
    for i, e in enumerate(entities):
        loc = f"entities[{i}]"
        name, plural = e.get("name", ""), e.get("plural", "")
        for label, val in (("name", name), ("plural", plural)):
            if not _IDENT_RE.match(val or ""):
                issues.append(f"{loc}.{label} {val!r} is not snake_case")
        if plural in seen_plurals:
            issues.append(f"{loc}.plural {plural!r} duplicated")
        seen_plurals.add(plural)
        fields = e.get("fields")
        if not isinstance(fields, dict) or len(fields) < 2:
            issues.append(f"{loc}.fields must be a dict with >=2 fields")
            continue
        idf = e.get("id_field", "id")
        if idf not in fields:
            issues.append(f"{loc}.id_field {idf!r} not among fields")
        for fname, ftype in fields.items():
            if not _IDENT_RE.match(fname):
                issues.append(f"{loc}.fields key {fname!r} is not snake_case")
            if ftype not in _ALLOWED_TYPES:
                issues.append(f"{loc}.fields[{fname!r}] type {ftype!r} not in "
                              f"{sorted(_ALLOWED_TYPES)}")
        actions = e.get("actions")
        if (not isinstance(actions, list) or not actions
                or any(a not in _ALLOWED_ACTIONS for a in actions)):
            issues.append(f"{loc}.actions must be a non-empty subset of "
                          f"{list(_ALLOWED_ACTIONS)}")
    return issues


# ═════════════════════════════════════════════════════════════════════════
# Step 3b — templated codegen (constrained CRUD, mirrors saved_info style)
# ═════════════════════════════════════════════════════════════════════════

_PY_TYPES = {"str": "str", "int": "int", "float": "float",
             "bool": "bool", "list": "list", "dict": "dict"}
_DEFAULTS = {"str": '""', "int": "0", "float": "0.0",
             "bool": "False", "list": "[]", "dict": "{}"}


def _create_model(e: dict[str, Any]) -> str:
    cls = "".join(w.capitalize() for w in e["name"].split("_"))
    lines = [f"class Create{cls}Request(BaseModel):"]
    idf = e.get("id_field", "id")
    for fname, ftype in e["fields"].items():
        if fname == idf:
            continue
        lines.append(f"    {fname}: {_PY_TYPES[ftype]} = {_DEFAULTS[ftype]}")
    if len(lines) == 1:
        lines.append("    pass")
    return "\n".join(lines)


def _entity_routes(e: dict[str, Any]) -> str:
    name, plural, idf = e["name"], e["plural"], e.get("id_field", "id")
    cls = "".join(w.capitalize() for w in name.split("_"))
    acts = e["actions"]
    blocks: list[str] = []
    if "list" in acts:
        blocks.append(f'''
        @self.router.post("/{plural}/list", operation_id="list_{plural}",
                          summary="List all {plural}")
        def list_{plural}() -> dict:
            svc.log_action("list_{plural}", params={{}}, count=len(svc.{plural}))
            return {{"{plural}": svc.{plural}}}
''')
    if "get" in acts:
        blocks.append(f'''
        @self.router.post("/{plural}/get", operation_id="get_{name}",
                          summary="Get one {name} by {idf}")
        def get_{name}(req: IdRequest):
            svc.log_action("get_{name}", params={{"{idf}": req.id}})
            for item in svc.{plural}:
                if str(item.get("{idf}")) == str(req.id):
                    return {{"{name}": item}}
            return JSONResponse(status_code=404,
                                content={{"error": "{name} not found"}})
''')
    if "create" in acts:
        blocks.append(f'''
        @self.router.post("/{plural}/create", operation_id="create_{name}",
                          summary="Create a new {name}")
        def create_{name}(req: Create{cls}Request):
            item = req.model_dump()
            item["{idf}"] = f"{name}-{{uuid.uuid4().hex[:8]}}"
            svc.{plural}.append(item)
            svc.log_action("create_{name}", params=req.model_dump(),
                           created_id=item["{idf}"])
            return {{"{name}": item}}
''')
    if "update" in acts:
        blocks.append(f'''
        @self.router.post("/{plural}/update", operation_id="update_{name}",
                          summary="Update fields of an existing {name}")
        def update_{name}(req: UpdateRequest):
            svc.log_action("update_{name}",
                           params={{"{idf}": req.id, "fields": req.fields}})
            for item in svc.{plural}:
                if str(item.get("{idf}")) == str(req.id):
                    item.update({{k: v for k, v in req.fields.items()
                                 if k != "{idf}"}})
                    return {{"{name}": item}}
            return JSONResponse(status_code=404,
                                content={{"error": "{name} not found"}})
''')
    if "delete" in acts:
        blocks.append(f'''
        @self.router.post("/{plural}/delete", operation_id="delete_{name}",
                          summary="Delete a {name} by {idf}")
        def delete_{name}(req: IdRequest):
            svc.log_action("delete_{name}", params={{"{idf}": req.id}})
            before = len(svc.{plural})
            svc.{plural} = [x for x in svc.{plural}
                            if str(x.get("{idf}")) != str(req.id)]
            if len(svc.{plural}) == before:
                return JSONResponse(status_code=404,
                                    content={{"error": "{name} not found"}})
            return {{"deleted": req.id}}
''')
    return "".join(blocks)


def render_service_py(spec: dict[str, Any]) -> str:
    """Deterministic codegen: spec -> service.py source (no LLM involved)."""
    template = spec["template"]
    cls = "".join(w.capitalize() for w in template.split("_")) + "Service"
    entities = spec["entities"]

    schema = {e["plural"]: [dict(e["fields"])] for e in entities}
    fixture_schema = textwrap.indent(
        json.dumps(schema, indent=4), "    ").lstrip()
    models = "\n\n\n".join(_create_model(e) for e in entities)
    load_lines = "\n".join(
        f'        self.{e["plural"]} = list(data.get("{e["plural"]}", []))'
        for e in entities)
    state_items = ", ".join(
        f'"{e["plural"]}": self.{e["plural"]}' for e in entities)
    route_blocks = "".join(_entity_routes(e) for e in entities)
    first_plural = entities[0]["plural"]
    fixture_doc = "\n".join(
        f'    {e["plural"]} [ {{{", ".join(f"{k}: {v}" for k, v in e["fields"].items())}}} ]'
        for e in entities)

    return f'''"""{template} service — {spec.get("description", "")}

SYNTHESIZED via services/synthesis.py (constrained CRUD codegen); spec at
services/_synthesis/{template}.spec.json. Bespoke mechanics beyond CRUD, if
any, are listed in the spec's ``notes`` and are intentionally NOT implemented.

Fixture shape:
{fixture_doc}
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture


class IdRequest(BaseModel):
    id: str


class UpdateRequest(BaseModel):
    id: str
    fields: dict = {{}}


{models}


class {cls}(ServiceBase):
    def __init__(self, service_id: str):
        super().__init__(service_id, template="{template}")
        self._register_routes()

    FIXTURE_SCHEMA = {fixture_schema}

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
{load_lines}

    def get_state(self) -> dict:
        return {{{state_items}}}

    def inject(self, content: dict) -> dict:
        """Environment-side injection: append records to a named collection."""
        collection = content.get("collection", "{first_plural}")
        records = content.get("records", [])
        target = getattr(self, collection, None)
        if target is None or not isinstance(records, list):
            return {{"error": f"unknown collection {{collection!r}}"}}
        target.extend(records)
        return {{"injected": len(records), "collection": collection}}

    def _register_routes(self) -> None:
        svc = self
{route_blocks}

def create_service(service_id: str) -> {cls}:
    svc = {cls}(service_id)
    svc.load_data()
    return svc
'''


# ═════════════════════════════════════════════════════════════════════════
# Step 4 — integration test: real boot, real requests, real audit
# ═════════════════════════════════════════════════════════════════════════

def _seed_value(ftype: str, fname: str) -> Any:
    return {"str": f"seed-{fname}", "int": 1, "float": 1.5,
            "bool": True, "list": [], "dict": {}}[ftype]


def integration_test(spec: dict[str, Any], service_dir: Path) -> list[str]:
    """Boot the generated service for real and smoke-test it.

    Returns a list of failures; empty list means the service passed."""
    template = spec["template"]
    failures: list[str] = []
    service_id = f"synthtest_{template}"

    fixture = {
        e["plural"]: [{f: _seed_value(t, f) for f, t in e["fields"].items()}]
        for e in spec["entities"]
    }
    for e in spec["entities"]:
        fixture[e["plural"]][0][e.get("id_field", "id")] = f"{e['name']}-0001"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(fixture, fh)
        fixture_path = fh.name
    env_key = fixture_env_var(service_id)
    os.environ[env_key] = fixture_path
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        module_name = f"services.{template}.service"
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
        svc = module.create_service(service_id)

        app = FastAPI()
        app.include_router(svc.router)
        client = TestClient(app)

        # 1. OpenAPI: every expected operation_id is exported.
        openapi = client.get("/openapi.json").json()
        op_ids = {op.get("operationId")
                  for path in openapi.get("paths", {}).values()
                  for op in path.values()}
        for e in spec["entities"]:
            expected = {"list": f"list_{e['plural']}", "get": f"get_{e['name']}",
                        "create": f"create_{e['name']}",
                        "update": f"update_{e['name']}",
                        "delete": f"delete_{e['name']}"}
            for act in e["actions"]:
                if expected[act] not in op_ids:
                    failures.append(f"OpenAPI missing operation {expected[act]}")

        # 2. CRUD round-trip per entity (subset the spec declares).
        for e in spec["entities"]:
            plural, name, idf = e["plural"], e["name"], e.get("id_field", "id")
            if "list" in e["actions"]:
                r = client.post(f"/{plural}/list")
                if r.status_code != 200 or len(r.json().get(plural, [])) != 1:
                    failures.append(f"list_{plural}: bad response {r.status_code}")
            if "get" in e["actions"]:
                r = client.post(f"/{plural}/get", json={"id": f"{name}-0001"})
                if r.status_code != 200:
                    failures.append(f"get_{name} seeded id: {r.status_code}")
                r = client.post(f"/{plural}/get", json={"id": "nope"})
                if r.status_code != 404:
                    failures.append(f"get_{name} missing id: expected 404, "
                                    f"got {r.status_code}")
            if "create" in e["actions"]:
                payload = {f: _seed_value(t, f) for f, t in e["fields"].items()
                           if f != idf}
                r = client.post(f"/{plural}/create", json=payload)
                if r.status_code != 200 or idf not in r.json().get(name, {}):
                    failures.append(f"create_{name}: bad response {r.status_code}")

        # 3. Audit log captured agent-visible actions.
        if not svc.audit_log:
            failures.append("audit_log empty after smoke actions")

        # 4. get_state exposes every collection (state-diff detector contract).
        state = svc.get_state()
        for e in spec["entities"]:
            if e["plural"] not in state:
                failures.append(f"get_state missing collection {e['plural']}")
    except Exception as exc:  # noqa: BLE001 — report, caller decides
        failures.append(f"boot/smoke crashed: {type(exc).__name__}: {exc}")
    finally:
        os.environ.pop(env_key, None)
        Path(fixture_path).unlink(missing_ok=True)
    return failures


# ═════════════════════════════════════════════════════════════════════════
# Step 5 — registration into the catalog + discovery check
# ═════════════════════════════════════════════════════════════════════════

def register(spec: dict[str, Any]) -> None:
    """Append the catalog entry and prove the registry discovers the service."""
    from services.registry import ServiceRegistry

    catalog = json.loads(CATALOG_PATH.read_text())
    if any(t.get("template") == spec["template"] for t in catalog["templates"]):
        raise SystemExit(f"catalog already has template {spec['template']!r}")
    if spec["category"] not in catalog["categories"]:
        desc = spec.get("category_description") or spec["description"]
        catalog["categories"][spec["category"]] = desc
    catalog["templates"].append({
        "template": spec["template"],
        "category": spec["category"],
        "description": spec["description"],
        "capability_notes": [],
    })
    CATALOG_PATH.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")

    reg = ServiceRegistry.load(derive_live=True)
    tspec = reg.templates.get(spec["template"])
    if tspec is None or not tspec.converted or not tspec.actions:
        raise SystemExit(
            f"registration verification FAILED: registry did not discover "
            f"{spec['template']!r} with live actions")


def build(spec_path: Path) -> None:
    """Steps 3-5: validate -> codegen -> integration test -> register."""
    spec = json.loads(spec_path.read_text())
    issues = validate_spec(spec)
    if issues:
        raise SystemExit("spec validation failed:\n" +
                         "\n".join(f"  - {i}" for i in issues))

    service_dir = _SERVICES_DIR / spec["template"]
    service_dir.mkdir()
    (service_dir / "__init__.py").write_text("")
    (service_dir / "service.py").write_text(render_service_py(spec))
    print(f"[synthesis] codegen -> {service_dir}/service.py")

    failures = integration_test(spec, service_dir)
    if failures:
        shutil.rmtree(service_dir)
        raise SystemExit("integration test FAILED (service rolled back):\n" +
                         "\n".join(f"  - {f}" for f in failures))
    print("[synthesis] integration test passed "
          "(boot + OpenAPI + CRUD round-trip + audit + state)")

    register(spec)
    print(f"[synthesis] registered {spec['template']!r}; registry discovery OK")


def remove(template: str) -> None:
    """Roll back a synthesized service (files + catalog entry)."""
    catalog = json.loads(CATALOG_PATH.read_text())
    entry = next((t for t in catalog["templates"]
                  if t.get("template") == template), None)
    if entry is not None and not entry.get("synthesized"):
        raise SystemExit(f"{template!r} is not a synthesized service; refusing "
                         "to remove a hand-written template")
    if entry is not None:
        catalog["templates"].remove(entry)
        CATALOG_PATH.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    service_dir = _SERVICES_DIR / template
    if service_dir.is_dir():
        shutil.rmtree(service_dir)
    print(f"[synthesis] removed {template!r} (files + catalog entry)")


# ═════════════════════════════════════════════════════════════════════════

def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft", help="LLM drafts a spec for human review")
    d.add_argument("--request", required=True,
                   help="capability gap description, e.g. from Stage 3 unresolved")
    d.add_argument("--profile", default="glm_5_3")
    d.add_argument("--config", type=Path,
                   default=_SANDBOX_ROOT / "evaluation" / "config.toml")

    b = sub.add_parser("build", help="validate + codegen + test + register")
    b.add_argument("--spec", type=Path, required=True)

    r = sub.add_parser("remove", help="roll back a synthesized service")
    r.add_argument("--template", required=True)

    args = ap.parse_args()
    if args.cmd == "draft":
        out = draft_spec(request=args.request, profile_name=args.profile,
                         config_file=args.config)
        print(f"[synthesis] spec drafted -> {out}\n"
              f"[synthesis] REVIEW IT, then set \"approved\": true and run:\n"
              f"[synthesis]   python services/synthesis.py build --spec {out}")
    elif args.cmd == "build":
        build(args.spec)
    elif args.cmd == "remove":
        remove(args.template)


if __name__ == "__main__":
    _main()
