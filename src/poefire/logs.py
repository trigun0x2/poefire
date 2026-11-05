"""poefire's own log file: <data folder>/logs/poefire.log (rotated, 5 x 2 MB).

Everything poefire logs goes there with a timestamp, the thread and the module; uncaught
exceptions in any thread too. "Verbose logging" (Settings) adds debug detail, e.g. why a
Client.txt line was ignored by the campaign tracker.
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any

LOG_NAME = "poefire.log"
CONSOLE_NAME = "console.log"  # the windowed app's stdout/stderr (prints, crashes)
MAX_BYTES, BACKUPS = 2 * 1024 * 1024, 4
FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

_handler: RotatingFileHandler | None = None
_saved_hooks: tuple[Any, Any] | None = None  # the hooks setup() replaced


def log_dir(data_dir: Path) -> Path:
    return data_dir / "logs"


def setup(data_dir: Path, verbose: bool = False, console: bool = True) -> Path | None:
    """Log to the file (and to stderr when there's a console). Safe to call again: only the
    first call adds handlers. Returns the log file, or None if it can't be written."""
    global _handler
    root = logging.getLogger()
    if _handler is None:
        path = log_dir(data_dir) / LOG_NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _handler = RotatingFileHandler(
                path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True
            )
        except OSError:
            return None
        _handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(_handler)
        if console and sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            stream.setLevel(logging.WARNING)
            root.addHandler(stream)
        root.setLevel(logging.INFO)
        for noisy in ("httpx", "httpcore", "uvicorn.access", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.captureWarnings(True)
        _install_excepthooks()
    set_verbose(verbose)
    return Path(_handler.baseFilename)


def set_verbose(on: bool) -> None:
    logging.getLogger("poefire").setLevel(logging.DEBUG if on else logging.INFO)
    if _handler is not None:
        _handler.setLevel(logging.DEBUG if on else logging.INFO)


def teardown() -> None:
    """Remove the file handler (tests)."""
    global _handler
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
        _handler = None
    global _saved_hooks
    if _saved_hooks is not None:
        sys.excepthook, threading.excepthook = _saved_hooks
        _saved_hooks = None


def current_file() -> Path | None:
    return Path(_handler.baseFilename) if _handler is not None else None


def _install_excepthooks() -> None:
    global _saved_hooks
    crash = logging.getLogger("poefire.crash")
    previous, previous_thread = sys.excepthook, threading.excepthook
    _saved_hooks = (previous, previous_thread)

    def excepthook(t: type[BaseException], e: BaseException, tb: TracebackType | None) -> None:
        if not issubclass(t, KeyboardInterrupt):
            crash.critical("uncaught exception", exc_info=(t, e, tb))
        previous(t, e, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        name = args.thread.name if args.thread is not None else "?"
        crash.critical(
            "uncaught exception in thread %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )  # fmt: skip
        previous_thread(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
