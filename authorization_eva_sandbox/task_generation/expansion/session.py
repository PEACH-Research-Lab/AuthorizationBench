"""One command per original case: overview, one-by-one approval, background builds.

`session` walks each feasible option of an original case in the terminal.
Approving an option starts its build in the background right away, logs
it to build_<target>/build.log, and on macOS opens a Terminal window that
follows the log. When every option has been decided, the session watches
the builds it started and prints the results. `status` shows every build.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from authorization_eva_sandbox.task_generation.expansion import build as build_mod
from authorization_eva_sandbox.task_generation.expansion import domain_survey
from authorization_eva_sandbox.task_generation.expansion import run as run_mod
from authorization_eva_sandbox.task_generation.expansion.originals import (
    OriginalCase,
    find_original_cases,
)

REPO_ROOT = build_mod.REPO_ROOT
STEP_ORDER = ("plan", "files", "checks", "leak", "review", "review_repair", "install",
              "smoke", "diagnosis")

Input = Callable[[str], str]
Launcher = Callable[..., dict[str, Any]]


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ── launching ───────────────────────────────────────────────────────────────

def global_flags(args) -> list[str]:
    return [
        "--survey-profile", args.survey_profile,
        "--writer-profile", args.writer_profile,
        "--writer-fallback-profile", args.writer_fallback_profile,
        "--critic-profile", args.critic_profile,
        "--config-file", str(args.config_file),
    ]


def open_log_window(log: Path, title: str) -> bool:
    """macOS only: a Terminal window that follows one build log."""
    if sys.platform != "darwin":
        return False
    command = f"printf '\\\\033]0;{title}\\\\007'; tail -n +1 -f {shlex.quote(str(log))}"
    script = f'tell application "Terminal" to do script "{command}"'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.returncode == 0


def launch_build(store: run_mod.SurveyStore, case: OriginalCase, target: str, args, *,
                 window: bool = True) -> dict[str, Any]:
    """Start `build --auto` for one approved option as a detached process."""
    build = build_mod.Build(store.root, case, target)
    build.dir.mkdir(parents=True, exist_ok=True)
    log = build.dir / "build.log"
    cmd = [sys.executable, "-u", "-m", "authorization_eva_sandbox.task_generation.expansion",
           *global_flags(args), "build", "--task", case.task_id, "--target", target, "--auto"]
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {build.child_id} build started {_now()} =====\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True,
                                env=dict(os.environ, PYTHONUNBUFFERED="1"))
    info = {"pid": proc.pid, "log": str(log), "started": _now(), "cmd": cmd}
    info["window"] = open_log_window(log, build.child_id) if window else False
    (build.dir / "launch.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


# ── status ──────────────────────────────────────────────────────────────────

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        # A build this session launched and that has exited stays a zombie
        # (and answers kill 0) until it is reaped.
        if os.waitpid(int(pid), os.WNOHANG)[0] != 0:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _log_error(build: build_mod.Build) -> str | None:
    launch = build.dir / "launch.json"
    if not launch.is_file():
        return None
    log = Path(json.loads(launch.read_text(encoding="utf-8")).get("log", ""))
    if not log.is_file():
        return None
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    for line in reversed(tail):
        if line.startswith(("RuntimeError", "ValueError", "KeyError", "TypeError",
                            "FileNotFoundError", "json.decoder", "Traceback")):
            return line[:160]
    return None


def build_status(build: build_mod.Build) -> dict[str, Any]:
    steps = build.state()["steps"]
    launch_path = build.dir / "launch.json"
    launch = json.loads(launch_path.read_text(encoding="utf-8")) if launch_path.is_file() else {}
    running = pid_alive(launch.get("pid"))
    done = [s for s in STEP_ORDER if s in steps]
    remaining = [s for s in STEP_ORDER if s not in steps]
    chk, leak, rev = build.load("checks") or {}, build.load("leak") or {}, build.load("review") or {}
    smoke = (steps.get("smoke") or {}).get("result") or {}
    diagnosis = (steps.get("diagnosis") or {}).get("verdict")
    install_problems = (steps.get("install") or {}).get("problems")
    if running:
        state = f"running: {remaining[0] if remaining else 'finishing'}"
    elif install_problems:
        state = "install failed"
    elif "smoke" in steps:
        state = "done"
    elif "install" in steps:
        state = "installed, no smoke run"
    elif "review" in steps:
        state = "needs attention" if not build_mod.build_passed(build) else "awaiting install"
    elif "plan" in steps and not steps["plan"].get("approved"):
        state = "plan awaiting approval"
    else:
        error = _log_error(build)
        state = f"stopped before {remaining[0]}" + (f" ({error})" if error else "")
    return {
        "child": build.child_id,
        "parent": build.case.task_id,
        "state": state,
        "running": running,
        "last_step": done[-1] if done else "",
        "checks": ("pass" if not chk.get("final") else f"{len(chk['final'])} issues") if chk else "",
        "leak": ("pass" if leak.get("pass") else "flagged") if leak else "",
        "review": ("pass" if rev.get("pass") else "not passed") if rev else "",
        "completion": smoke.get("completion", ""),
        "violation": smoke.get("violation", ""),
        "diagnosis": diagnosis or "",
        "report": str(build.dir / "report.md") if (build.dir / "report.md").is_file() else "",
    }


def all_builds(store: run_mod.SurveyStore, task: str | None = None) -> list[build_mod.Build]:
    cases = {c.task_id: c for c in find_original_cases()}
    base = store.root / store.axis
    out = []
    if not base.is_dir():
        return out
    for parent_dir in sorted(base.iterdir()):
        if task and parent_dir.name != task:
            continue
        case = cases.get(parent_dir.name)
        if case is None:
            continue
        for build_dir in sorted(parent_dir.glob("build_*")):
            target = build_dir.name.removeprefix("build_")
            build = build_mod.Build(store.root, case, target)
            if build.state_path.is_file():
                out.append(build)
    return out


def render_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no builds yet"
    cols = ("child", "state", "checks", "leak", "review", "completion", "violation", "diagnosis")
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    widths["state"] = min(widths["state"], 48)
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    out = [line, "-" * len(line)]
    for r in rows:
        out.append("  ".join(str(r.get(c, ""))[:widths[c]].ljust(widths[c]) for c in cols))
    return "\n".join(out)


def watch(builds: list[build_mod.Build], *, interval: float = 15, out=print,
          sleep=time.sleep) -> list[dict[str, Any]]:
    """Print the status table whenever it changes, until no build is running."""
    last = None
    try:
        while True:
            rows = [build_status(b) for b in builds]
            table = render_status(rows)
            if table != last:
                out(f"\n[{_now()}]\n{table}")
                last = table
            if not any(r["running"] for r in rows):
                return rows
            sleep(interval)
    except KeyboardInterrupt:
        out("\nstopped watching; the builds keep running. Check them with `status`.")
        return [build_status(b) for b in builds]


def final_summary(rows: list[dict[str, Any]]) -> str:
    lines = ["", "=== results ==="]
    for r in rows:
        lines.append(f"{r['child']}: {r['state']}")
        if r["completion"] or r["violation"]:
            lines.append(f"  GPT-4.1: completion={r['completion']} violation={r['violation']}")
        if r["diagnosis"]:
            lines.append(f"  why violation=no: {r['diagnosis']}")
        if r["report"]:
            lines.append(f"  report: {r['report']}")
    return "\n".join(lines)


# ── session ─────────────────────────────────────────────────────────────────

def _started(build: build_mod.Build) -> bool:
    return build.done("files") or pid_alive(
        json.loads((build.dir / "launch.json").read_text()).get("pid")
        if (build.dir / "launch.json").is_file() else None)


def run_session(store: run_mod.SurveyStore, case: OriginalCase, args, *,
                ask: Input = input, out=print, launcher: Launcher = launch_build,
                window: bool = True) -> tuple[list[build_mod.Build], bool]:
    """Returns (builds started in this session, whether the human quit)."""
    config_file = Path(args.config_file)
    run_mod.ensure_overview(store, case, survey_profile=args.survey_profile,
                            writer_profile=args.writer_profile, config_file=config_file,
                            writer_fallback_profile=args.writer_fallback_profile, out=out)
    out("\n".join(run_mod.render_case_header(store, case)))
    started: list[build_mod.Build] = []
    for option in run_mod.sorted_options(store.load(case.task_id, "options") or {}):
        if not option.get("feasible"):
            continue
        letter = option["domain"]
        build = build_mod.Build(store.root, case, letter)
        entry = store.entry(case.task_id) or {}
        targets = entry.get("targets") or []
        if _started(build):
            out(f"\n{build.child_id}: already built or building ({build_status(build)['state']})")
            continue
        if letter in (entry.get("rejected") or []):
            out(f"\n{build.child_id}: rejected earlier, skipped")
            continue
        if letter not in targets and domain_survey.slots_left(case, targets) <= 0:
            out(f"\n{case.task_id}: no slots left "
                f"(max {domain_survey.MAX_OTHER_DOMAINS} other domains); remaining options skipped")
            break
        while True:
            out("\n".join(run_mod.render_option(store, case, option)))
            plan = build.load("plan")
            if plan:
                out(build_mod.render_plan(build, plan))
            else:
                out(f"{build.child_id}: no plan yet (the last attempt was invalid). "
                    "[c]omment writes it again; approving needs a plan.")
            choice = ask(f"{build.child_id}: [a]pprove and build  [c]omment on the plan  "
                         "[r]eject  [s]kip  [q]uit > ").strip().lower()
            if choice == "q":
                return started, True
            if choice == "s":
                break
            if choice == "r":
                reason = ask("Reason (optional): ").strip()
                run_mod.reject_target(store, case, letter, reason, out=out)
                break
            if choice == "c":
                comment = ask("Comment: ").strip()
                if comment:
                    try:
                        run_mod.revise_plan(store, case, letter, comment,
                                            writer_profile=args.writer_profile,
                                            fallback_profile=args.writer_fallback_profile,
                                            config_file=config_file, out=out)
                    except (run_mod.ExpansionError, ValueError) as exc:
                        out(str(exc))
                continue
            if choice == "a":
                try:
                    run_mod.approve_target(store, case, letter, out=out)
                except run_mod.ExpansionError as exc:
                    out(str(exc))
                    break
                info = launcher(store, case, letter, args, window=window)
                where = "a new Terminal window and " if info.get("window") else ""
                out(f"{build.child_id}: build started (pid {info['pid']}); "
                    f"follow it in {where}{info['log']}")
                started.append(build)
                break
    return started, False


def cmd_session(store: run_mod.SurveyStore, args, *, ask: Input = input) -> int:
    say = lambda *a: print(*a, flush=True)  # noqa: E731
    cases = find_original_cases()
    if args.task:
        cases = [c for c in cases if c.task_id == args.task]
        if not cases:
            raise SystemExit(f"{args.task} is not an original case; run `list` to see them")
    started: list[build_mod.Build] = []
    for case in cases:
        entry = store.entry(case.task_id) or {}
        if args.all and entry.get("status") == "none":
            continue
        if args.all and domain_survey.slots_left(case, entry.get("targets")) <= 0:
            continue
        try:
            new, quit_ = run_session(store, case, args, ask=ask, out=say,
                                     window=not args.no_window)
        except run_mod.ExpansionError as exc:
            say(str(exc))
            continue
        started.extend(new)
        if quit_:
            break
    if not started:
        say("no builds started in this session")
        return 0
    if args.no_watch:
        say("builds are running in the background; check them with `status`")
        return 0
    rows = watch(started, out=say)
    say(final_summary(rows))
    return 0


def cmd_status(store: run_mod.SurveyStore, args) -> int:
    builds = all_builds(store, args.task)
    if args.watch:
        rows = watch(builds, out=lambda *a: print(*a, flush=True))
        print(final_summary(rows))
        return 0
    rows = [build_status(b) for b in builds]
    print(render_status(rows))
    return 0
