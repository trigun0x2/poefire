"""Entry point for the Windows build (PyInstaller).

Two executables share one bundle:
- poefire.exe     console app: the full CLI (`poefire.exe set-session`, `poefire.exe runs`, ...);
                  with no arguments it opens the UI.
- poefire-ui.exe  no console window: always opens the UI. Its output goes to poefire.log in the
                  data folder (%APPDATA%\\poefire), since there is no console to print to.
"""

import sys
from pathlib import Path

from poefire.cli import main
from poefire.config import default_data_dir

if __name__ == "__main__":
    argv = sys.argv[1:]
    if Path(sys.executable).stem.lower() == "poefire-ui":
        log_dir = default_data_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "poefire.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stderr = log
        argv = argv or ["ui", "--window"]
    raise SystemExit(main(argv))
