"""Entry point for the Windows build (PyInstaller).

The executables share one bundle:
- poefire.exe      no console window: opens the UI. Its output goes to logs\\console.log in
                   the data folder (%APPDATA%\\poefire), since there is no console to print to;
                   the app's log is logs\\poefire.log there.
- poefire-cli.exe  console app: the full CLI (`poefire-cli.exe set-session`, `... runs`);
                   with no arguments it opens the UI.
"""

import sys
from pathlib import Path

from poefire.cli import main
from poefire.config import default_data_dir

if __name__ == "__main__":
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        from poefire.winfix import unblock_dlls

        unblock_dlls(Path(sys._MEIPASS))  # type: ignore[attr-defined]
    argv = sys.argv[1:]
    if Path(sys.executable).stem.lower() == "poefire":  # the windowed app
        # No console: prints and crash tracebacks go to logs/console.log (this start's; the
        # previous start's is kept as console.1.log). poefire's own log is logs/poefire.log.
        log_dir = default_data_dir() / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            current = log_dir / "console.log"
            if current.exists():
                current.replace(log_dir / "console.1.log")
            out = open(current, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
            sys.stdout = sys.stderr = out
        except OSError:
            pass  # e.g. another poefire.exe is running and holds the file
        argv = argv or ["ui", "--window"]
    raise SystemExit(main(argv))
