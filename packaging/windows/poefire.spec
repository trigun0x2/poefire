# PyInstaller spec for the Windows release: one folder, executables sharing it:
#   poefire.exe      the app (its own window, no console), with the app icon
#   poefire-cli.exe  the command line (console), no icon
# Build from the repository root:  pyinstaller packaging/windows/poefire.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parents[1]  # noqa: F821 (SPECPATH is set by PyInstaller)

a = Analysis(  # noqa: F821
    [str(root / "packaging" / "windows" / "launcher.py")],
    pathex=[str(root / "src")],
    # sigstore: its built-in trust roots (the updater checks update attestations with them)
    datas=[(str(root / "src" / "poefire" / "web" / "static"), "poefire/web/static")]
    + collect_data_files("sigstore"),
    # uvicorn picks its protocol/loop implementations by import string at runtime
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("poefire")
    + collect_submodules("sigstore"),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)  # noqa: F821

icon = str(root / "src" / "poefire" / "web" / "static" / "favicon.ico")  # Ember Gem app icon (same art as static/logo.png)
common = dict(exclude_binaries=True, debug=False, strip=False, upx=False)
app = EXE(pyz, a.scripts, [], name="poefire", console=False, icon=icon, **common)  # noqa: F821
cli = EXE(pyz, a.scripts, [], name="poefire-cli", console=True, **common)  # noqa: F821

COLLECT(app, cli, a.binaries, a.datas, name="poefire", strip=False, upx=False)  # noqa: F821
