"""Command-line entry point (Phase 1; the web UI arrives in Phase 3)."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import os
import re
import signal
import sys
import threading
import webbrowser
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from poefire import logs, wiring
from poefire.api.client import ApiError
from poefire.api.oauth import NotLoggedInError, OAuthError, TokenStore, login
from poefire.api.session import SessionError, SessionStore
from poefire.config import Config, default_config_path, default_data_dir
from poefire.loot.diff import ItemDelta
from poefire.loot.recorder import LootRecorder, LootResult, Trigger
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


def _print_deltas(deltas: Sequence[ItemDelta], indent: str = "  ") -> None:
    if not deltas:
        print(f"{indent}(no change)")
    for d in deltas:
        print(f"{indent}{d.quantity:+6d}  {d.name}")


def _print_loot(r: LootResult) -> None:
    if not r.complete:
        print(f"[    loot] snapshot failed: {r.error}", flush=True)
    elif r.baseline:
        print(f"[    loot] baseline snapshot taken ({r.trigger.value})", flush=True)
    else:
        print(f"[    loot] {r.kind} delta ({r.trigger.value}):", flush=True)
        _print_deltas(r.deltas)


_ACCOUNT_ERRORS = (wiring.ConfigError, NotLoggedInError, ApiError, SessionError, httpx.HTTPError)


def _loot_recorder(cfg: Config) -> LootRecorder | None:
    """None (with a message) when loot tracking isn't set up; run tracking still works."""
    try:
        return wiring.recorder(cfg, wiring.account_api(cfg))
    except wiring.ConfigError as e:
        print(f"loot tracking off: {e}")
        return None


def cmd_track(args: argparse.Namespace) -> int:
    cfg = _config(args)
    log_path = cfg.resolve_log_path()
    if log_path is None:
        print("Client.txt not found; pass --log or set log_path in config.toml", file=sys.stderr)
        return 2
    rec = _loot_recorder(cfg)
    with Store(cfg.db_path) as store:
        service = TrackerService(store, log_path, cfg.zone_rules(), cfg.characters)
        service.handlers.append(_print_notice)
        if rec is not None:
            rec.handlers.append(_print_loot)
            service.handlers.append(rec.on_notice)
            rec.start()
            print("loot tracking on: snapshots on map entry and on return to hideout")
        service.start()
        print(f"tailing {log_path} -> {cfg.db_path} (Ctrl+C to stop)", flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        try:
            service.run_forever(stop, cfg.poll_interval)
        finally:
            if rec is not None:
                rec.stop()
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    cfg = _config(args)
    try:
        tokens = wiring.token_manager(cfg)

        def open_browser(url: str) -> None:
            print(f"Opening your browser to log in. If it doesn't open, visit:\n{url}")
            webbrowser.open(url)

        result = login(tokens.oauth, open_browser)
    except (wiring.ConfigError, OAuthError) as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    tokens.set_tokens(result)
    print(f"logged in as {result.username or '?'} (scopes: {result.scope})")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    cfg = _config(args)
    TokenStore(cfg.token_path).clear()
    SessionStore(cfg.session_path).clear()
    print("logged out (saved OAuth token and POESESSID deleted)")
    return 0


_POESESSID_RE = re.compile(r"[0-9a-f]{32}")


def cmd_set_session(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(
        "Paste your POESESSID cookie (pathofexile.com > browser dev tools > Storage/Application "
        "> Cookies).\nIt is a full login session for your account: never share it. "
        "Input is hidden."
    )
    value = getpass.getpass("POESESSID: ").strip()
    if not value:
        print("nothing entered; unchanged", file=sys.stderr)
        return 1
    if not _POESESSID_RE.fullmatch(value):
        print("warning: a POESESSID is usually 32 hex characters; saving anyway")
    SessionStore(cfg.session_path).save(value)
    print(f"saved to {cfg.session_path} (readable only by you)")
    if cfg.auth != "session" or not cfg.account_name:
        print('set auth = "session" and account_name in config.toml to use it')
        return 0
    try:
        chars = wiring.account_api(cfg).list_characters()
    except _ACCOUNT_ERRORS as e:
        print(f"saved, but the check failed: {e}", file=sys.stderr)
        return 1
    names = ", ".join(f"{c.get('name')} ({c.get('league')})" for c in chars[:8])
    print(f"works: {len(chars)} characters: {names}")
    return 0


def cmd_tabs(args: argparse.Namespace) -> int:
    cfg = _config(args)
    try:
        poe = wiring.account_api(cfg)
        if not cfg.league:
            raise wiring.ConfigError("set league in config.toml")
        tabs = poe.list_stashes(cfg.league)
    except _ACCOUNT_ERRORS as e:
        print(e, file=sys.stderr)
        return 1

    def show(tab: dict[str, object], depth: int = 0) -> None:
        print(f"{'  ' * depth}{tab.get('name', '')!s:<30} {tab.get('type', '')!s:<20} {tab['id']}")
        for child in tab.get("children") or []:  # type: ignore[attr-defined]
            show(child, depth + 1)

    for tab in tabs:
        show(tab)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = _config(args)
    try:
        poe = wiring.account_api(cfg)
        with Store(cfg.db_path) as store:
            rec = LootRecorder(wiring.snapshotter(cfg, poe), lambda: store)
            result = rec.record(Trigger.MANUAL)
    except _ACCOUNT_ERRORS as e:
        print(e, file=sys.stderr)
        return 1
    _print_loot(result)
    return 0 if result.complete else 1


def cmd_loot(args: argparse.Namespace) -> int:
    cfg = _config(args)
    with Store(cfg.db_path) as store:
        runs = [store.get_run(args.run)] if args.run else store.list_runs(limit=args.limit)
        for run in reversed([r for r in runs if r is not None]):
            print(_fmt_run(run))
            between = store.run_loot(run.id, kind="between")
            if between:
                print("  before the run (hideout):")
                _print_deltas(between, "    ")
            print("  loot:")
            _print_deltas(store.run_loot(run.id), "    ")
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


def _add_overlay(rt: Any, main_window: Any) -> None:
    """The in-game overlay window (Windows): started once the main window is up (after
    pywebview has set the process's DPI mode), closed with it."""
    from poefire.web import hud, overlay_window

    def state() -> dict[str, Any]:
        with Store(rt.cfg.db_path) as store:
            return hud.overlay_state(rt, store)

    def save_position(x: int, y: int) -> None:
        with Store(rt.cfg.db_path) as store:
            store.set_json(overlay_window.POSITION_KEY, {"x": x, "y": y})

    def shown() -> None:
        if rt.overlay is not None:
            return
        try:
            with Store(rt.cfg.db_path) as store:
                position = store.get_json(overlay_window.POSITION_KEY)
            cfg = rt.cfg
            rt.overlay = overlay_window.start(
                state, position, save_position, cfg.overlay_enabled, cfg.overlay_opacity
            )
        except Exception as e:  # the overlay is optional; the app works without it
            print(f"overlay unavailable: {e!r}")

    def closed() -> None:
        if rt.overlay is not None:
            with contextlib.suppress(Exception):
                rt.overlay.stop()

    main_window.events.shown += shown
    main_window.events.closed += closed


def cmd_apply_update(args: argparse.Namespace) -> int:
    """Internal: run by the updater from the staged new version (see poefire.updater)."""
    from poefire.updater import apply_update

    return apply_update(args.source, args.target, args.pid, args.log)


def cmd_verify(args: argparse.Namespace) -> int:
    """Check a downloaded release zip against its build attestation (.sigstore.json)."""
    import hashlib

    from poefire import provenance

    zip_path: Path = args.zip
    bundle = args.bundle or zip_path.with_name(zip_path.name + ".sigstore.json")
    sha = args.sha256
    try:
        if not sha:
            digest = hashlib.sha256()
            with open(zip_path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            sha = digest.hexdigest()
        provenance.verify(bundle.read_bytes(), sha, zip_path.name,
                          beta=args.beta, offline=args.offline)  # fmt: skip
    except (OSError, provenance.ProvenanceError) as e:
        print(f"NOT verified: {e}")
        return 1
    branches = "master or dev" if args.beta else "master"
    print(f"verified: {zip_path.name} (sha256 {sha}) was built by "
          f"{provenance.WORKFLOW} on {branches}")  # fmt: skip
    return 0


def cmd_debug_report(args: argparse.Namespace) -> int:
    """Save a debug report (as Settings > Diagnostics does) without opening the app."""
    from poefire import diagnostics
    from poefire.web.runtime import AppRuntime

    cfg = _config(args)
    rt = AppRuntime(cfg, recorder=None, config_path=args.config or default_config_path())
    try:
        path = diagnostics.save(rt)
    finally:
        rt.stop()
    print(f"debug report saved: {path}")
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(f"log: {cfg.resolve_log_path() or '(not found)'}")
    print(f"db:  {cfg.db_path}")
    return 0


def _show_running(url: str) -> bool:
    """Whether poefire already answers at `url`; if so, asks it to bring its window up."""
    import httpx

    try:
        r = httpx.get(url + "api/status", timeout=2)
        if r.status_code != 200 or "loot" not in r.json():
            return False
        httpx.post(url + "api/window/show", headers={"X-Poefire": "1"}, timeout=2)
    except (httpx.HTTPError, ValueError):
        return False
    return True


def cmd_ui(args: argparse.Namespace) -> int:
    """Run the tracker with the web UI (in the browser, or a native window with --window)."""
    import uvicorn

    from poefire.web.app import create_app
    from poefire.web.runtime import AppRuntime

    url = f"http://127.0.0.1:{args.port}/"
    if _show_running(url):
        # poefire already runs: it shows its window; a second copy would track everything
        # twice into the same database (and couldn't take the port anyway)
        if not args.window and not args.no_browser:
            webbrowser.open(url)
        print(f"poefire is already running at {url}")
        return 0
    cfg = _config(args)
    rt = AppRuntime(cfg, config_path=args.config or default_config_path())
    if rt.log_path is None:
        print("Client.txt not found; run tracking is off (pass --log or set log_path)")
    elif rt.loot.reason:
        print(f"loot tracking off: {rt.loot.reason}")

    def open_ui() -> None:
        webbrowser.open(url)

    server = uvicorn.Server(
        uvicorn.Config(create_app(rt), host="127.0.0.1", port=args.port, log_level="warning")
    )
    rt.start()
    try:
        if args.window:
            try:
                import webview  # pywebview: Edge WebView2 on Windows
            except ImportError:
                print("--window needs pywebview: uv sync --extra window; opening the browser")
            else:
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                try:
                    from poefire.web.window import MIN_HEIGHT, MIN_WIDTH, WindowControls

                    controls = WindowControls()
                    # No OS title bar: the page draws its own (see web/window.py). Only the
                    # bar's background drags the window, not the buttons/selects inside it.
                    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
                    window = webview.create_window(
                        "poefire", url, width=1360, height=860,
                        min_size=(MIN_WIDTH, MIN_HEIGHT), frameless=True, easy_drag=False,
                        js_api=controls, background_color="#111317",
                    )  # fmt: skip
                    controls._attach(window)
                    rt.request_exit = controls.close  # updater: close the window to install
                    rt.request_show = controls._bring_to_front
                    _add_overlay(rt, window)
                    # the packaged exe carries the icon itself; this covers running from source
                    icon = Path(__file__).parent / "web" / "static" / "favicon.ico"
                    webview.start(icon=str(icon))
                except Exception as e:  # e.g. .NET/WebView2 can't load on this machine
                    print(f"native window failed ({e!r}); opening the browser instead")
                    rt.request_exit = lambda: setattr(server, "should_exit", True)
                    rt.request_show = open_ui
                    webbrowser.open(url)
                    while thread.is_alive():  # keep serving; join in steps so Ctrl+C works
                        thread.join(0.5)
                    return 0
                server.should_exit = True
                thread.join(timeout=5)
                return 0
        rt.request_exit = lambda: setattr(server, "should_exit", True)
        rt.request_show = open_ui
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        print(f"poefire UI at {url} (Ctrl+C to stop)")
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="poefire")
    p.add_argument("--config", type=Path, help="path to config.toml")
    p.add_argument("--log", type=Path, help="path to Client.txt")
    p.add_argument("--db", type=Path, help="path to SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("track", help="tail Client.txt and record runs").set_defaults(fn=cmd_track)

    ap = sub.add_parser("apply-update")  # internal, used by the self-updater
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--target", type=Path, required=True)
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.set_defaults(fn=cmd_apply_update)

    rp = sub.add_parser("replay", help="parse a whole log file and list runs")
    rp.add_argument("logfile", type=Path)
    rp.add_argument("--limit", type=int, default=10_000)
    rp.add_argument("-v", "--verbose", action="store_true", help="print every notice")
    rp.set_defaults(fn=cmd_replay)

    lp = sub.add_parser("runs", help="list recorded runs")
    lp.add_argument("--limit", type=int, default=50)
    lp.set_defaults(fn=cmd_runs)

    sub.add_parser("paths", help="show resolved paths").set_defaults(fn=cmd_paths)
    vp = sub.add_parser("verify", help="check a downloaded release zip against its signed "
                        "build attestation")  # fmt: skip
    vp.add_argument("zip", type=Path)
    vp.add_argument("--bundle", type=Path, help="default: <zip>.sigstore.json next to it")
    vp.add_argument("--beta", action="store_true", help="also accept beta builds (from dev)")
    vp.add_argument("--sha256", help="the zip's SHA-256, instead of reading the zip")
    vp.add_argument("--offline", action="store_true", help="use the built-in Sigstore trust root")
    vp.set_defaults(fn=cmd_verify)
    sub.add_parser("debug-report", help="save a debug report .zip to send when something "
                   "doesn't work").set_defaults(fn=cmd_debug_report)  # fmt: skip

    sub.add_parser("set-session", help="save your POESESSID cookie (session auth)").set_defaults(
        fn=cmd_set_session
    )
    sub.add_parser("login", help="log in with a registered OAuth app (auth = oauth)").set_defaults(
        fn=cmd_login
    )
    sub.add_parser("logout", help="forget the saved POESESSID and OAuth token").set_defaults(
        fn=cmd_logout
    )
    sub.add_parser("tabs", help="list stash tabs in the configured league").set_defaults(
        fn=cmd_tabs
    )
    sub.add_parser("snapshot", help="take a snapshot now and show the change").set_defaults(
        fn=cmd_snapshot
    )
    up = sub.add_parser("ui", help="tracker with the web UI")
    up.add_argument("--port", type=int, default=8460)
    up.add_argument("--window", action="store_true", help="native window (needs pywebview)")
    up.add_argument("--no-browser", action="store_true", help="don't open a browser")
    up.set_defaults(fn=cmd_ui)

    lt = sub.add_parser("loot", help="show loot per run")
    lt.add_argument("--run", help="run id (default: recent runs)")
    lt.add_argument("--limit", type=int, default=10)
    lt.set_defaults(fn=cmd_loot)
    return p


def _ensure_streams() -> None:
    """A windowed exe (or pythonw) has no stdout/stderr: sys.stdout is None, and anything
    printing to it, or uvicorn setting up its log format, would crash. Send them nowhere."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_streams()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["ui", "--window"]  # double-clicked: open the app
    args = build_parser().parse_args(argv)
    if args.fn is not cmd_apply_update:  # that one writes its own log
        try:
            verbose = Config.load(args.config).debug_logging
        except Exception:  # a broken config is reported by the command itself
            verbose = False
        logs.setup(default_data_dir(), verbose)
        logging.getLogger("poefire").info("command: %s", " ".join(argv) or "(none)")
    code: int = args.fn(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
