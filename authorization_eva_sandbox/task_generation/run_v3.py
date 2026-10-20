"""run_v3 — the single entry point for the v3 generation chain.

    Stage 1   seed -> policy skeleton + oracle_user   vignettes/_policy_skeletons/
    Stage 2a  story                                   vignettes/_stories/
    Stage 2b  script                                  vignettes/_scripts/
    Stage 2c  agent surface                           vignettes/_surfaces/
    Stage 3   assembly blueprint                      vignettes/_blueprints/
    Stage 4   Fill Envelope + World Realization + compile/boot/seal   tasks/T*/
    Stage 5   empirical acceptance                    tasks/T*/acceptance/

This file ORCHESTRATES only: Stage 1 is driven in-process (the 40 lines that
used to live in the retired pipeline.py); Stages 2-5 are invoked through
their own CLIs as subprocesses so each keeps its exact behaviour and exit
semantics. Strict sequencing: a stage whose gate fails stops the run and
names the artifact to inspect. Progress goes to the terminal only.

    ./au312/bin/python -m authorization_eva_sandbox.task_generation.run_v3 \
        --seed-id S0010 --from 2a --to 5 --force
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]          # authorization_eva_sandbox/
REPO_ROOT = ROOT.parent
for p in (str(ROOT), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from authorization_eva_sandbox.task_generation.shared.utils import (  # noqa: E402
    output_path_for_seed,
    read_json,
    write_json,
)
from authorization_eva_sandbox.task_generation.shared.artifact_gate import (  # noqa: E402
    attach_gate_receipt,
    gate_receipt_matches,
    registry_contract_snapshot,
    stored_gate_passed,
)

STAGES = ["1", "2a", "2b", "2c", "3", "4", "5"]
PKG = "authorization_eva_sandbox.task_generation.stages"
MOD = {
    "2a": f"{PKG}.seed_to_vignette.stage2",
    "2b": f"{PKG}.seed_to_vignette.stage2_script",
    "2c": f"{PKG}.seed_to_vignette.stage2_surface",
    "3": f"{PKG}.assembly.stage3",
    "4": f"{PKG}.materialize.stage4",
    "5": f"{PKG}.accept.stage5",
}
ARTIFACT_DIR = {"1": "_policy_skeletons", "2a": "_stories", "2b": "_scripts",
                "2c": "_surfaces"}


def _artifact_path(stage: str, stem: str) -> Path:
    vignettes = ROOT / "vignettes"
    if stage == "3":
        return vignettes / f"{stem}.json"
    return vignettes / ARTIFACT_DIR[stage] / f"{stem}.json"


def _log(msg: str) -> None:
    print(f"[run_v3] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def _find_seed(seeds: list[dict], seed_id: str) -> dict:
    for s in seeds:
        if s.get("seed_id") == seed_id:
            return s
    raise SystemExit(f"seed {seed_id} not found")


# ── Stage 1 (in-process; ported from pipeline.py's --stop-after-stage1) ────

def run_stage1(*, seed: dict, taxonomy: dict, stem: str, args: argparse.Namespace,
               force: bool) -> bool:
    from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage1 import (
        generate_oracle_user_for_seed,
        generate_policy_skeleton_for_seed,
        run_oracle_user_stage,
        run_policy_skeleton_stage,
    )

    vignette_path = ROOT / "vignettes" / f"{stem}.json"      # naming anchor only
    ps_path = ROOT / "vignettes" / "_policy_skeletons" / f"{stem}.json"
    audit_dir = Path(args.audit_dir) / stem

    stage1_inputs = {"seed": seed, "taxonomy": taxonomy}
    stage1_models = {
        "generator": args.stage1_profile,
        "critic": args.stage1_critic_profile,
        "oracle": args.stage1_profile,
    }
    if (not force and ps_path.exists()
            and gate_receipt_matches(
                read_json(ps_path),
                stage="1",
                inputs=stage1_inputs,
                models=stage1_models,
            )):
        _log(f"stage=1 status=exists (current gate receipt) {ps_path}")
        return True

    def save_audit(name: str, data: Any) -> None:
        write_json(audit_dir / name, data)

    _log("stage=1 generating policy skeleton")
    ps = generate_policy_skeleton_for_seed(
        seed=seed, taxonomy=taxonomy, vignette_path=vignette_path,
        profile_name=args.stage1_profile, config_file=Path(args.config_file),
        max_tokens=args.stage1_max_tokens, policy_skeleton_path=ps_path)
    result, ps = run_policy_skeleton_stage(
        seed=seed, policy_skeleton=ps, vignette_path=vignette_path, taxonomy=taxonomy,
        config_file=Path(args.config_file),
        critic_profile=args.stage1_critic_profile,
        repair_profile=args.stage1_profile, max_rounds=args.max_rounds,
        max_tokens=args.stage1_max_tokens, do_repair=True, save_audit_fn=save_audit,
        policy_skeleton_path=ps_path, save_passing_audit=False)
    _log(f"stage=1 skeleton gate pass={result.get('pass')}")
    if not result.get("pass"):
        return False

    _log("stage=1 generating oracle_user")
    oracle_user = generate_oracle_user_for_seed(
        seed=seed, profile_name=args.stage1_profile, config_file=Path(args.config_file))
    oracle_result, oracle_user = run_oracle_user_stage(
        seed=seed, oracle_user=oracle_user, config_file=Path(args.config_file),
        repair_profile=args.stage1_profile, max_rounds=args.max_rounds,
        do_repair=True, save_audit_fn=save_audit, save_passing_audit=False)
    _log(f"stage=1 oracle_user gate pass={oracle_result.get('pass')} "
         f"user={oracle_user.get('user_name', '?')}")
    if not oracle_result.get("pass"):
        return False
    ps["oracle_user"] = oracle_user
    attach_gate_receipt(
        ps,
        stage="1",
        passed=True,
        inputs=stage1_inputs,
        models=stage1_models,
    )
    write_json(ps_path, ps)
    _log(f"stage=1 status=pass {ps_path}")
    return True


# ── Capability gaps → synthesis loop (ClawEnvKit's missing-services idea) ───
#
# Two detection points feed one handler: the post-2a capability check (cheap,
# early — before 2b/2c spend effort) and Stage 3's `unresolved` list (precise,
# action-level, the fallback). Gaps split into two kinds:
#   missing_services   → `synthesis.py draft` per gap, then STOP for human
#                        review of the spec (approved:false until a person
#                        flips it); approved specs are built automatically
#                        before the next Stage 2a/3 run.
#   missing_behaviours → an existing template lacks a behaviour the story
#                        relies on. Codegen cannot produce behaviour, so STOP
#                        and offer: hand-extend the service, or rewrite the
#                        story (`--from 2a --force` picks the gaps up as a
#                        critique automatically).
# Nothing is approved or built without a human touching the spec file.

SYNTH = ROOT / "services" / "synthesis.py"
SPEC_DIR = ROOT / "services" / "_synthesis"


def _gaps_path(stem: str) -> Path:
    return ROOT / "vignettes" / "_stories" / "_audits" / stem / "capability_gaps.json"


def _run_capability_check(*, stem: str, args: argparse.Namespace) -> dict:
    from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
        check_story_capabilities,
    )
    story = read_json(ROOT / "vignettes" / "_stories" / f"{stem}.json")
    result = check_story_capabilities(
        story=story, profile_name=args.stage2_critic_profile,
        config_file=Path(args.config_file))
    audit = ROOT / "vignettes" / "_stories" / "_audits" / stem / "capability_check.json"
    write_json(audit, result)
    _log(f"stage=2a capability_check pass={result['pass']} "
         f"covered={len(result['covered'])} missing_services={len(result['missing_services'])} "
         f"missing_behaviours={len(result['missing_behaviours'])} -> {audit}")
    return result


def _gaps_from_blueprint(stem: str) -> dict:
    """Classify Stage 3 `unresolved` entries into the two gap kinds."""
    path = ROOT / "vignettes" / "_blueprints" / f"{stem}.json"
    if not path.exists():
        return {"missing_services": [], "missing_behaviours": []}
    doc = read_json(path)
    gaps = {"missing_services": [], "missing_behaviours": []}
    for u in (doc.get("assembly_blueprint") or {}).get("unresolved") or []:
        if not isinstance(u, dict) or u.get("confirmed") is not True:
            continue
        reason = str(u.get("reason") or u.get("why_no_match") or "")
        quote = str(
            u.get("rationale") or u.get("rationale_quote") or u.get("need") or ""
        )
        if u.get("type") == "missing_service":
            gaps["missing_services"].append({"platform": quote, "request": f"{quote}: {reason}"})
        else:
            gaps["missing_behaviours"].append({"template": str(u.get("carrier") or "(see reason)"),
                                               "behaviour": reason,
                                               "story_quote": quote})
    return gaps


def handle_gaps(gaps: dict, *, stem: str, args: argparse.Namespace, source: str) -> None:
    """Draft specs for missing services, record every gap, print what a human
    must do next. Always followed by STOP in the caller."""
    write_json(_gaps_path(stem), {"source": source, **gaps})
    drafted = []
    for g in gaps.get("missing_services") or []:
        req = str(g.get("request") or g.get("platform") or "").strip()
        if not req:
            continue
        _log(f"gap: missing service — {req[:120]}")
        rc = _run([sys.executable, str(SYNTH), "draft", "--request", req,
                   "--profile", args.stage4_profile, "--config", args.config_file])
        if rc == 0:
            drafted.append(req)
    for g in gaps.get("missing_behaviours") or []:
        _log(f"gap: missing behaviour on {g.get('template')} — "
             f"{str(g.get('behaviour'))[:140]}")
    print("\n[run_v3] CAPABILITY GAPS (from %s) — human decision needed:" % source)
    if drafted:
        print(f"  {len(drafted)} service spec(s) drafted under {SPEC_DIR}/ (approved: false).")
        print("  Review each spec, set \"approved\": true, then rerun this seed with "
              "--from 3 (or --from 2a); approved specs are built automatically first.")
    if gaps.get("missing_behaviours"):
        print(f"  {len(gaps['missing_behaviours'])} existing template(s) lack a behaviour "
              "the story relies on. Either:")
        print("    (a) extend the service by hand, then rerun --from 3; or")
        print("    (b) rerun --from 2a --force: the story is rewritten against these gaps "
              "(they are passed as a critique automatically).")
    print(f"  Gap record: {_gaps_path(stem)}")


def build_approved_specs() -> bool:
    """Build every approved-but-unbuilt spec (idempotent). False on a failure."""
    ok = True
    for spec_path in sorted(SPEC_DIR.glob("*.spec.json")):
        spec = read_json(spec_path)
        template = str(spec.get("template") or "")
        if spec.get("approved") is not True or not template:
            continue
        if (ROOT / "services" / template / "service.py").exists():
            continue
        _log(f"synthesis: building approved spec {spec_path.name}")
        rc = _run([sys.executable, str(SYNTH), "build", "--spec", str(spec_path)])
        if rc != 0:
            _log(f"synthesis: build FAILED for {spec_path.name}")
            ok = False
    return ok


# ── Stages 2-5 (subprocess; each stage's own CLI) ───────────────────────────

def _run(cmd: list[str]) -> int:
    _log("exec " + " ".join(cmd[2:]))
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return proc.returncode


def _common(args: argparse.Namespace) -> list[str]:
    return ["--seed-id", args.seed_id, "--seed-file", args.seed_file,
            "--taxonomy-file", args.taxonomy_file, "--config-file", args.config_file]


def _stage_receipt_inputs(
    stage: str,
    *,
    stem: str,
    seed: dict,
) -> dict[str, Any]:
    from authorization_eva_sandbox.task_generation.shared.grounding import (
        render_contract_slice,
        render_mechanism_slice,
        resolve_trigger_letters,
    )
    from authorization_eva_sandbox.task_generation.stages.seed_to_vignette.stage2 import (
        _seed_stage2_prompt_fields,
        stage2_contract_fingerprint,
    )

    vignettes = ROOT / "vignettes"
    policy_skeleton = read_json(
        vignettes / "_policy_skeletons" / f"{stem}.json"
    )
    seed_fields = _seed_stage2_prompt_fields(seed)
    letters, unresolved = resolve_trigger_letters(
        seed_fields.get("situational_trigger")
    )
    if unresolved:
        raise ValueError(f"unresolved trigger(s): {unresolved!r}")
    trigger_contract = render_contract_slice(letters, include_declares=False)
    if stage == "2a":
        return {
            "seed": seed_fields,
            "policy_skeleton": policy_skeleton,
            "oracle_user": policy_skeleton.get("oracle_user") or {},
            "trigger_contract": trigger_contract,
        }
    story = read_json(vignettes / "_stories" / f"{stem}.json")
    if stage == "2b":
        return {
            "policy_skeleton": policy_skeleton,
            "story": story,
            "seed": seed_fields,
            "trigger_contract": render_mechanism_slice(letters),
            "prompt_contract": stage2_contract_fingerprint("2b"),
        }
    script = read_json(vignettes / "_scripts" / f"{stem}.json")
    if stage == "2c":
        return {
            "policy_skeleton": policy_skeleton,
            "story": story,
            "script": script,
            "trigger_contract": trigger_contract,
            "prompt_contract": stage2_contract_fingerprint("2c"),
        }
    if stage == "3":
        from authorization_eva_sandbox.task_generation.stages.assembly.stage3 import (
            stage3_contract_fingerprint,
        )
        surface = read_json(vignettes / "_surfaces" / f"{stem}.json")
        return {
            "policy_skeleton": policy_skeleton,
            "story": story,
            "script": script,
            "surface": surface,
            "registry": registry_contract_snapshot(),
            "prompt_contract": stage3_contract_fingerprint(),
        }
    raise ValueError(f"unsupported cached artifact stage {stage!r}")


def _stage_receipt_models(
    stage: str,
    args: argparse.Namespace,
) -> dict[str, str]:
    if stage in {"2a", "2b", "2c"}:
        return {
            "generator": args.stage2_profile,
            "critic": args.stage2_critic_profile,
        }
    if stage == "3":
        return {
            "generator": args.stage3_profile,
            "critic": args.stage3_critic_profile,
        }
    raise ValueError(f"unsupported cached artifact stage {stage!r}")


def _artifact_passed(
    stage: str,
    stem: str,
    *,
    seed: dict,
    args: argparse.Namespace,
) -> bool | None:
    """Return whether the canonical artifact has a current passing receipt."""
    path = _artifact_path(stage, stem)
    if not path.exists():
        return None
    doc = read_json(path)
    if stage == "3":
        from authorization_eva_sandbox.task_generation.shared.canonical_vignette import (
            canonical_vignette_issues,
        )
        return (
            stored_gate_passed(doc, stage="3")
            and not canonical_vignette_issues(doc, expected_stem=stem)
        )
    return gate_receipt_matches(
        doc,
        stage=stage,
        inputs=_stage_receipt_inputs(stage, stem=stem, seed=seed),
        models=_stage_receipt_models(stage, args),
    )


def run_stage2(stage: str, *, stem: str, seed: dict,
               args: argparse.Namespace, force: bool) -> bool:
    if not force and _artifact_passed(
        stage, stem, seed=seed, args=args
    ):
        _log(f"stage={stage} status=exists (gate passed) — use --force to regenerate")
        return True
    cmd = [sys.executable, "-m", MOD[stage], *_common(args),
           "--profile", args.stage2_profile,
           "--critic-profile", args.stage2_critic_profile,
           "--max-rounds", str(args.max_rounds)]
    if stage == "2a":
        # A recorded gap list (from a previous capability check / Stage 3)
        # becomes the critique the story is rewritten against — once.
        gaps_path = _gaps_path(stem)
        if gaps_path.exists() and force:
            gaps = read_json(gaps_path)
            issues = [{"problem": f"Missing service: {g.get('platform')} — {g.get('request')}",
                       "repair_instruction": "Use a platform kind this sandbox provides, or "
                                             "state plainly that a new one is required."}
                      for g in gaps.get("missing_services") or []]
            issues += [{"problem": f"Missing behaviour on {g.get('template')}: "
                                   f"{g.get('behaviour')} (story: {g.get('story_quote')})",
                        "repair_instruction": "Design the friction/temptation with an action "
                                              "this template really has, or drop the mechanic."}
                       for g in gaps.get("missing_behaviours") or []]
            if issues:
                crit_path = gaps_path.with_name("capability_critique.json")
                write_json(crit_path, {"issues": issues})
                cmd += ["--critique-file", str(crit_path)]
                _log(f"stage=2a rewriting against {len(issues)} recorded capability gap(s)")
            gaps_path.unlink()
    rc = _run(cmd)
    passed = rc == 0 and _artifact_passed(
        stage, stem, seed=seed, args=args
    ) is True
    _log(f"stage={stage} status={'pass' if passed else 'FAIL'} "
         f"artifact={_artifact_path(stage, stem)}")
    if passed and stage == "2a" and not args.skip_capability_check:
        check = _run_capability_check(stem=stem, args=args)
        if not check["pass"]:
            handle_gaps(check, stem=stem, args=args, source="capability_check")
            return False
    return passed


def run_stage3(*, stem: str, seed: dict,
               args: argparse.Namespace, force: bool) -> bool:
    if not force and _artifact_passed(
        "3", stem, seed=seed, args=args
    ):
        _log("stage=3 status=exists (gate passed) — use --force to regenerate")
        return True
    cmd = [sys.executable, "-m", MOD["3"], *_common(args),
           "--profile", args.stage3_profile,
           "--critic-profile", args.stage3_critic_profile,
           "--max-rounds", str(args.max_rounds)]
    rc = _run(cmd)
    passed = rc == 0 and _artifact_passed(
        "3", stem, seed=seed, args=args
    ) is True
    _log(f"stage=3 status={'pass' if passed else 'FAIL'} "
         f"artifact={_artifact_path('3', stem)}")
    if not passed:
        gaps = _gaps_from_blueprint(stem)
        if gaps["missing_services"] or gaps["missing_behaviours"]:
            handle_gaps(gaps, stem=stem, args=args, source="stage3_unresolved")
    return passed


def run_stage4(*, stem: str, args: argparse.Namespace) -> bool:
    """One Stage 4 invocation: realize, compile, boot, seal, promote."""
    bundle = Path(args.out_root) / ("T" + stem[1:])
    cmd = [sys.executable, "-m", MOD["4"], *_common(args), "--out-root", args.out_root,
           "--profile", args.stage4_profile,
           "--critic-profile", args.stage4_critic_profile]
    _log("stage=4 start")
    rc = _run(cmd)
    if rc != 0:
        attempts = Path(args.out_root) / ".stage4-attempts" / bundle.name
        _log(f"stage=4 status=FAIL attempts={attempts}")
        return False
    _log(f"stage=4 status=pass bundle={bundle}")
    return True


def run_stage5(*, stem: str, args: argparse.Namespace) -> bool:
    bundle = Path(args.out_root) / ("T" + stem[1:])
    cmd = [sys.executable, "-m", MOD["5"], *_common(args), "--out-root", args.out_root,
           "--actor-profile", args.actor_profile, "--oracle-profile", args.oracle_profile]
    rc = _run(cmd)
    _log(f"stage=5 status={'pass' if rc == 0 else 'FAIL'} "
         f"report={bundle / '_generation' / 'acceptance.json'}")
    return rc == 0


# ── main ────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed-id", required=True)
    ap.add_argument("--from", dest="from_stage", choices=STAGES, default="1")
    ap.add_argument("--to", dest="to_stage", choices=STAGES, default="5")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate stages 1/2a/2b/2c/3 even if their gate already passed. "
                         "Stages 4 and 5 always run.")
    ap.add_argument("--seed-file", default=str(ROOT / "seeds" / "seed.json"))
    ap.add_argument("--taxonomy-file",
                    default=str(ROOT / "task_generation" / "taxonomy" / "taxonomy_options.json"))
    ap.add_argument("--config-file", default=str(ROOT / "evaluation" / "config.toml"))
    ap.add_argument("--out-root", default=str(ROOT / "tasks"))
    ap.add_argument("--audit-dir", default=str(ROOT / "surgery_failures"))
    # Profiles: defaults mirror each stage CLI's own defaults.
    ap.add_argument("--stage1-profile", default="glm_5_3")
    ap.add_argument("--stage1-max-tokens", type=int, default=25000)
    ap.add_argument("--stage2-profile", default="gpt5_6_sol",
                    help="Stage 2 generator/repair profile")
    ap.add_argument("--stage3-profile", default="gpt5_6_sol",
                    help="Stage 3 negotiation/generator/repair profile")
    ap.add_argument("--stage4-profile", default="opus_4_8", help="Stage 4 content profile")
    ap.add_argument("--stage1-critic-profile", default="gpt5_6_sol")
    ap.add_argument("--stage2-critic-profile", default="opus_4_8")
    ap.add_argument("--stage3-critic-profile", default="opus_4_8")
    ap.add_argument("--stage4-critic-profile", default="gpt5_6_sol")
    ap.add_argument("--actor-profile", default="gpt4_1_mini")
    ap.add_argument("--oracle-profile", default="mimo_v2_6_pro_oracle")
    ap.add_argument("--max-rounds", type=int, default=2, help="Stage 1/2/3 repair rounds")
    ap.add_argument("--skip-capability-check", action="store_true",
                    help="Do not run the post-2a capability check (Stage 3 still reports "
                         "unresolved gaps).")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    i0, i1 = STAGES.index(args.from_stage), STAGES.index(args.to_stage)
    if i0 > i1:
        raise SystemExit("--from is after --to")
    seeds = read_json(Path(args.seed_file))
    taxonomy = read_json(Path(args.taxonomy_file))
    seed = _find_seed(seeds, args.seed_id)
    stem = output_path_for_seed(seed, taxonomy).stem
    _log(f"seed={args.seed_id} stem={stem} stages={STAGES[i0:i1 + 1]} force={args.force}")

    for stage in STAGES[i0:i1 + 1]:
        if stage in ("2a", "3") and not build_approved_specs():
            _log(f"STOP before stage={stage}: an approved synthesis spec failed to build")
            return 1
        if stage == "1":
            ok = run_stage1(seed=seed, taxonomy=taxonomy, stem=stem, args=args, force=args.force)
        elif stage in ("2a", "2b", "2c"):
            ok = run_stage2(
                stage, stem=stem, seed=seed, args=args, force=args.force
            )
        elif stage == "3":
            ok = run_stage3(
                stem=stem, seed=seed, args=args, force=args.force
            )
        elif stage == "4":
            ok = run_stage4(stem=stem, args=args)
        else:
            ok = run_stage5(stem=stem, args=args)
        if not ok:
            _log(f"STOP at stage={stage} seed={args.seed_id}")
            print(json.dumps({"seed_id": args.seed_id, "stem": stem,
                              "stopped_at": stage, "pass": False}, indent=2))
            return 1

    _log(f"DONE seed={args.seed_id} stages={STAGES[i0:i1 + 1]}")
    print(json.dumps({"seed_id": args.seed_id, "stem": stem,
                      "completed": STAGES[i0:i1 + 1], "pass": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
