"""Command-line entry point (Phase 1; the web UI arrives in Phase 3)."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from poefire.config import Config
from poefire.service import TrackerService
from poefire.store import Store
from poefire.tracker import Notice, Run, RunNotice, SafeZoneEntered


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_run(run: Run, now: datetime | None = None) -> str:
    return (
        f"{run.started_at:%Y-%m-%d %H:%M:%S}  {run.map_name:<28.28} "
        f"lvl {run.area_level:>2}  {_fmt_duration(run.active_seconds(now)):>8}  "
        f"deaths {run.deaths}  {run.status.value}"
    )


def _print_notice(n: Notice) -> None:
    if isinstance(n, RunNotice):
        print(f"[{n.kind.value:>8}] {_fmt_run(n.run)}", flush=True)
    elif isinstance(n, SafeZoneEntered):
        print(f"[    safe] {n.ts:%H:%M:%S} entered {n.zone} ({n.kind.value})", flush=True)


def _print_runs(runs: Sequence[Run]) -> None:
    if not runs:
        print("no runs")
        return
    for r in runs:
        print(_fmt_run(r))
    total = sum(r.active_seconds() for r in runs)
    avg = total / len(runs)
    deaths = sum(r.deaths for r in runs)
    print(
        f"-- {len(runs)} runs, active {_fmt_duration(total)}, "
        f"avg {_fmt_duration(avg)}, deaths {deaths}"
    )


def _config(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    if args.log:
        cfg.log_path = args.log
    if args.db:
        cfg.db_path = args.db
    return cfg


def cmd_track(args: argparse.Namespace) -> int:
    cfg = _config(args)
    log_path = cfg.resolve_log_path()
    if log_path is None:
        print("Client.txt not found; pass --log or set log_path in config.toml", file=sys.stderr)
        return 2
    with Store(cfg.db_path) as store:
        service = TrackerService(store, log_path, cfg.zone_rules(), cfg.characters)
        service.handlers.append(_print_notice)
        service.start()
        print(f"tailing {log_path} -> {cfg.db_path} (Ctrl+C to stop)", flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        service.run_forever(stop, cfg.poll_interval)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Parse a whole log file into a throwaway (or given) DB and print the runs found."""
    cfg = _config(args)
    with Store(args.db or ":memory:") as store:
        service = TrackerService(store, args.logfile, cfg.zone_rules(), cfg.characters)
        if args.verbose:
            service.handlers.append(_print_notice)
        service.start(from_beginning=True)
        service.poll()
        _print_runs(list(reversed(store.list_runs(limit=args.limit))))
    current = service.tracker.run
    if current is not None:
        print(f"(run in progress: {current.map_name}, {current.status.value})")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    cfg = _config(args)
    with Store(cfg.db_path) as store:
        _print_runs(list(reversed(store.list_runs(limit=args.limit))))
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(f"log: {cfg.resolve_log_path() or '(not found)'}")
    print(f"db:  {cfg.db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="poefire")
    p.add_argument("--config", type=Path, help="path to config.toml")
    p.add_argument("--log", type=Path, help="path to Client.txt")
    p.add_argument("--db", type=Path, help="path to SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("track", help="tail Client.txt and record runs").set_defaults(fn=cmd_track)

    rp = sub.add_parser("replay", help="parse a whole log file and list runs")
    rp.add_argument("logfile", type=Path)
    rp.add_argument("--limit", type=int, default=10_000)
    rp.add_argument("-v", "--verbose", action="store_true", help="print every notice")
    rp.set_defaults(fn=cmd_replay)

    lp = sub.add_parser("runs", help="list recorded runs")
    lp.add_argument("--limit", type=int, default=50)
    lp.set_defaults(fn=cmd_runs)

    sub.add_parser("paths", help="show resolved paths").set_defaults(fn=cmd_paths)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
