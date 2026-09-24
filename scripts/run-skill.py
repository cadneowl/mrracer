#!/usr/bin/env python
"""Run one skill exactly as radar would, by hand, and say what happened.

The gap this fills: when a skill fails inside radar and succeeds when you run
it in your terminal, the difference is never the command — it is the *launch*.
radar picks the working directory, strips its own credentials, exports a
default environment, applies the skill's `env:` and `env_unset:`, and pipes a
context bundle on stdin. Reproducing that by hand means guessing at five things
at once, and a guess that is wrong anywhere makes the result meaningless.

So this does not reimplement any of it. It imports radar's own
`CommandRunner._child_env`, `build_argv` and `stdin_provider_for`, which means
what it launches is what the board button launches, by construction.

    scripts/run-skill.py review --mr 3774/10567
    scripts/run-skill.py build-doctor --build "CI/128"
    scripts/run-skill.py review --mr 3774/10567 --no-context   # is it the prompt?
    scripts/run-skill.py review --env ANTHROPIC_API_KEY=sk-... # is it the auth?
    scripts/run-skill.py review --unset CLAUDE_CONFIG_DIR      # is it the config dir?
    scripts/run-skill.py review --print-env                    # launch nothing; just look

`--env`, `--unset` and `--clean` are the point: they are how you bisect a
launch. Each one is applied *after* radar's own rules, so it overrides them —
which is what you want when the question is "does it still fail without this?".

Secrets are never printed. A variable whose name looks like a credential is
shown as <set, N chars>, because the answer to "is it set?" is almost always
the whole question and the value never is.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar.commands import CommandRunner, build_argv  # noqa: E402
from radar.config import load_config  # noqa: E402
from radar.dotenv import candidates, load_dotenv  # noqa: E402

SECRETISH = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH")


def _shown(name: str, value: str) -> str:
    """A value, or the fact that there is one, for anything that looks secret."""
    if any(word in name.upper() for word in SECRETISH):
        return f"<set, {len(value)} chars>"
    return value


def _summarise(path: Path) -> None:
    """What the run's own event stream says — the half that never reaches stderr.

    A provider that refuses is the case this exists for: the CLI retries a 401
    quietly and exits non-zero having printed only whatever unrelated warning it
    happened to emit, so the reason is in here and nowhere else.
    """
    refusals: dict[str, int] = {}
    result = None
    events = 0
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        events += 1
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("subtype") == "api_retry":
            status = event.get("error_status") or ""
            label = f"HTTP {status} {event.get('error', '')}".strip()
            refusals[label] = refusals.get(label, 0) + 1
        elif event.get("type") == "result":
            result = event

    print(f"\nstream: {events} events -> {path}")
    if refusals:
        print("  the provider refused:")
        for label, count in refusals.items():
            print(f"    {label} x{count}")
        print("  (nothing was ever sent to a model — this is the reason, not stderr)")
    if result is not None:
        usage = result.get("usage") or {}
        print(
            f"  result: {result.get('subtype') or 'ok'}"
            f" · {result.get('num_turns', 0)} turns"
            f" · api {result.get('duration_api_ms', 0) / 1000:.1f}s"
            f" · ${result.get('total_cost_usd', 0):.4f}"
            f" · in {usage.get('input_tokens', 0):,}"
            f" · out {usage.get('output_tokens', 0):,}"
        )
    elif not refusals:
        print("  no result event and no refusals — the run stopped without reporting")


def _launch(argv, cwd, env, stdin_text, timeout, out_path):
    """Start it, wait for it, and return what happened.

    Separated from `main` only so `--bisect` can do it several times over; a
    single run goes through here too, so the two can never drift apart.
    """
    started = time.time()
    with out_path.open("w", encoding="utf-8") as sink:
        proc = subprocess.Popen(
            argv, cwd=cwd or None, env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=sink, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        killed = False
        try:
            _, stderr = proc.communicate(stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
            killed = True
    return proc.returncode, stderr, time.time() - started, killed


def _refusals(path: Path) -> dict[str, int]:
    """Every call the provider turned down, by reason."""
    found: dict[str, int] = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("subtype") == "api_retry":
            label = f"HTTP {event.get('error_status', '')} {event.get('error', '')}".strip()
            found[label] = found.get(label, 0) + 1
    return found


def _bisect(argv_, skill, base, stdin_text, seconds: int) -> int:
    """Run the same skill three ways and say which step breaks it.

    The ladder is the three things that can differ between a launch that works
    and one that does not: the environment, the flags, and the prompt. Each rung
    adds exactly one of them, so the first one that fails names the cause
    instead of leaving three suspects.

    Each is given a short clock on purpose. A provider that is going to refuse
    does it on the first call, in seconds; a run that gets past that is working,
    and killing it once it plainly is costs nothing and saves a five-dollar
    review nobody asked for.
    """
    bare = {k: v for k, v in base.items() if k in ("PATH", "HOME")}
    bare.update(dict(skill.env))
    plain = ["claude", "-p", "--output-format", "stream-json", "--verbose", "say OK"]

    rungs = [
        ("1. bare env, plain prompt", plain, bare, None),
        ("2. radar's env, plain prompt", plain, base, None),
        ("3. radar's env, radar's flags", argv_, base, None),
        ("4. everything, real prompt", argv_, base, stdin_text),
    ]
    print(
        f"\nEach rung adds one thing. {seconds}s each — a refusal arrives in the"
        " first second or not at all.\n"
    )
    print(f"{'':34s} {'exit':>5s} {'401s':>5s} {'secs':>6s}  what it means")
    culprit = None
    for label, this_argv, this_env, this_stdin in rungs:
        out = Path(f"/tmp/radar-bisect-{label[0]}.jsonl")
        code, _, took, killed = _launch(
            this_argv, skill.working_dir, this_env, this_stdin, seconds, out
        )
        refused = _refusals(out)
        n401 = sum(count for label_, count in refused.items() if "401" in label_)
        total = sum(refused.values())
        if total:
            verdict = "REFUSED — " + ", ".join(f"{k} x{v}" for k, v in refused.items())
            culprit = culprit or label
        elif killed:
            verdict = "still working when the clock ran out — no refusals"
        elif code == 0:
            verdict = "fine"
        else:
            verdict = f"exited {code}, but the provider never refused"
        print(f"{label:34s} {code:5d} {n401:5d} {took:6.1f}  {verdict}")

    print()
    if culprit is None:
        print("No rung was refused. The launch is not what is breaking it — run the "
              "full thing with --timeout 600 and look at the stream.")
    else:
        step = culprit[0]
        blame = {
            "1": "the credential itself: even a bare launch is refused, so this is the "
                 "token or the endpoint, nothing radar does",
            "2": "radar's environment: the same command works without it. Compare the "
                 "'+' and '-' lines above and bisect with --unset",
            "3": "the flags or the slash command — not the environment and not the prompt",
            "4": "the prompt: everything works until the real context is piped in",
        }[culprit[0]]
        print(f"First refused at rung {step}, so the cause is {blame}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one skill exactly as radar would.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("skill", help="a name from the 'skills:' list in the config")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mr", metavar="PROJECT/IID",
                        help="the merge request to run it for, e.g. 3774/10567")
    parser.add_argument("--build", metavar="JOB/NUMBER",
                        help="the Jenkins build to run it for, e.g. CI/128")
    parser.add_argument("--no-context", action="store_true",
                        help="pipe nothing on stdin — is it the size of the prompt?")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                        help="set a variable for the child (repeatable, wins over radar)")
    parser.add_argument("--unset", action="append", default=[], metavar="NAME",
                        help="drop a variable from the child (repeatable)")
    parser.add_argument("--clean", action="store_true",
                        help="start from PATH and HOME alone, not your shell")
    parser.add_argument("--print-env", action="store_true",
                        help="show the launch and exit without running anything")
    parser.add_argument("--timeout", type=int, default=0,
                        help="seconds before giving up (default: the skill's own)")
    parser.add_argument("--out", default="", help="where to keep the raw stream")
    parser.add_argument("--bisect", action="store_true",
                        help="run it four ways and say which one breaks it")
    args = parser.parse_args(argv)

    # radar reads a .env beside its config and in the working directory, and the
    # values land in its own environment before any child inherits them. Not
    # doing this here would launch with a different environment than the thing
    # being reproduced, which is the one mistake this script exists to avoid.
    for candidate in candidates(args.config):
        load_dotenv(candidate)

    config = load_config(args.config)
    skill = config.skill_by_name(args.skill)
    if skill is None:
        names = ", ".join(s.name for s in config.skills) or "none are declared"
        print(f"no skill named {args.skill!r} — the config has {names}", file=sys.stderr)
        return 2
    if skill.pipeline:
        steps = " -> ".join(" + ".join(stage) for stage in skill.pipeline)
        print(
            f"{args.skill!r} is a pipeline ({steps}), and this runs one command. "
            "Name one of its steps.",
            file=sys.stderr,
        )
        return 2

    runner = CommandRunner(skill, skill.name)

    # --- the context bundle, fetched the way the button fetches it ----------
    ctx: dict = {}
    stdin_text: str | None = None
    if args.mr:
        project_id, _, mr_iid = args.mr.partition("/")
        from radar.context import stdin_provider_for
        from radar.db import Database
        from radar.jira import extract_keys

        with Database(str(config.database_path)) as db:
            snap = db.get_snapshot(int(project_id), int(mr_iid))
        if snap is None:
            print(f"radar has no merge request {args.mr} — poll first", file=sys.stderr)
            return 2
        keys = extract_keys(
            [snap.get("title"), snap.get("source_branch"), snap.get("description")],
            config.jira.project_keys,
        )
        ctx = {k: snap.get(k, "") for k in
               ("web_url", "title", "author", "source_branch", "target_branch", "head_sha")}
        ctx.update({"project_id": project_id, "mr_iid": mr_iid,
                    "jira_keys": " ".join(keys), "jira_keys_csv": ",".join(keys)})
        provider = stdin_provider_for(skill.name, config, int(project_id), int(mr_iid), keys)
        if provider is not None and not args.no_context:
            print("fetching this skill's context…", flush=True)
            stdin_text = provider("", {}, "")
    elif args.build:
        job_name, _, number = args.build.partition("/")
        job = next((j for j in config.jenkins.jobs if j.name == job_name), None)
        ctx = {"jenkins_job": job_name, "build_number": number,
               "build_url": f"{job.url}/{number}/" if job else "", "title": job_name}

    source_root = skill.working_dir or ""
    argv_ = build_argv(skill.command, {**ctx, "source_root": source_root})

    # --- the environment, built by radar then bent by the flags -------------
    base = runner._child_env()
    if args.clean:
        base = {k: v for k, v in base.items() if k in ("PATH", "HOME")}
        base.update(dict(skill.env))
    for pair in args.env:
        name, _, value = pair.partition("=")
        base[name] = value
    for name in args.unset:
        base.pop(name, None)

    mine = dict(os.environ)
    differs = {k: v for k, v in base.items() if mine.get(k) != v}
    dropped = sorted(k for k in mine if k not in base)

    print(f"skill      : {skill.name} ({skill.label or skill.name})")
    print(f"command    : {shlex.join(argv_)}")
    print(f"working dir: {skill.working_dir or os.getcwd()}")
    print(f"stdin      : {len(stdin_text):,} characters"
          if stdin_text is not None else "stdin      : nothing piped")
    print(f"timeout    : {args.timeout or skill.timeout_seconds}s")
    print("env, where it differs from this shell:")
    for name in sorted(differs):
        print(f"  + {name}={_shown(name, differs[name])}")
    for name in dropped:
        print(f"  - {name}   (radar strips its own credentials)")
    for name in sorted(base):
        if any(word in name.upper() for word in ("ANTHROPIC", "CLAUDE")) and name not in differs:
            print(f"    {name}={_shown(name, base[name])}")
    if args.print_env:
        return 0
    if args.bisect:
        return _bisect(argv_, skill, base, stdin_text, args.timeout or 90)

    out_path = Path(args.out or f"/tmp/radar-{skill.name}-{int(time.time())}.jsonl")
    print(f"\nrunning…  (raw stream -> {out_path})", flush=True)
    code, stderr, took, killed = _launch(
        argv_, skill.working_dir, base, stdin_text,
        args.timeout or skill.timeout_seconds, out_path,
    )
    if killed:
        print(f"\nKILLED after {took:.0f}s")
    print(f"\nexit code  : {code}  after {took:.1f}s")
    print(f"stderr     : {stderr.strip()[-2000:] or '(empty)'}")
    _summarise(out_path)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
