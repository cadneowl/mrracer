"""radar command-line interface.

    python -m radar poll-once     one polling pass, then exit
    python -m radar serve         run the dashboard + background poller
    python -m radar recompute     re-derive all obligations from the event log
    python -m radar validate      check config.yaml and exit

Secrets come only from GITLAB_URL / GITLAB_TOKEN in the environment.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, gitlab_credentials, load_config
from .db import Database
from .poller import poll_once
from .service import recompute as run_recompute

log = logging.getLogger("radar")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )


def _make_source(config):
    from .gitlab_client import GitLabSource

    url, token = gitlab_credentials()
    return GitLabSource(url, token)


def cmd_poll_once(args) -> int:
    config = load_config(args.config)
    with Database(str(config.database_path)) as db:
        result = poll_once(db, config, _make_source(config))
        summary = run_recompute(db, config)
    print(
        f"polled {result.projects} project(s): {result.mrs_seen} MRs seen, "
        f"{result.new_events} new events."
    )
    print(
        "obligation states — "
        + ", ".join(f"{k.lower()}={v}" for k, v in summary["summary"].items())
    )
    return 0


def cmd_recompute(args) -> int:
    config = load_config(args.config)
    with Database(str(config.database_path)) as db:
        summary = run_recompute(db, config)
    print(f"recomputed {summary['obligations']} obligation(s) from the event log.")
    print(", ".join(f"{k.lower()}={v}" for k, v in summary["summary"].items()))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .web.app import create_app

    config = load_config(args.config)
    db_path = str(config.database_path)
    # Ensure schema exists before the first request.
    Database(db_path).close()

    app = create_app(config, db_path)

    scheduler = None
    try:
        source = _make_source(config)
        from .scheduler import make_scheduler

        scheduler = make_scheduler(config, db_path, lambda: source)
        scheduler.start()
        log.info("background poller started (every %d min)", config.gitlab.poll_interval_minutes)
    except ConfigError as exc:
        log.warning("background poller disabled: %s", exc.args[0].splitlines()[0])
        log.warning("serving read-only from existing data; set GITLAB_URL/GITLAB_TOKEN to poll")

    try:
        # Single worker on purpose: the poller (APScheduler) and the in-memory
        # command-job registry live in this process. Running multiple workers
        # would start N pollers and split job state across processes.
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
    return 0


def cmd_validate(args) -> int:
    config = load_config(args.config)
    print(f"config OK: {len(config.gitlab.projects)} project(s), {len(config.slas)} SLA rule(s).")
    print(f"default timezone: {config.calendar.default_timezone}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__)
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("poll-once", help="one polling pass then exit").set_defaults(func=cmd_poll_once)
    sub.add_parser("recompute", help="re-derive obligations from events").set_defaults(
        func=cmd_recompute
    )
    sub.add_parser("validate", help="validate config.yaml").set_defaults(func=cmd_validate)

    serve = sub.add_parser("serve", help="run dashboard + background poller")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
