"""Entry point for the Windows build (PyInstaller).

The executables share one bundle:
- poefire.exe      no console window: opens the UI. Its output goes to poefire.log in the data
                   folder (%APPDATA%\\poefire), since there is no console to print to.
- poefire-cli.exe  console app: the full CLI (`poefire-cli.exe set-session`, `... runs`);
                   with no arguments it opens the UI.
- poefire-ui.exe   the old name of poefire.exe, kept so older installs can update themselves.
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
    if Path(sys.executable).stem.lower() in ("poefire", "poefire-ui"):  # the windowed app
        log_dir = default_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "poefire.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = log
        argv = argv or ["ui", "--window"]
    raise SystemExit(main(argv))
