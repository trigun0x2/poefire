# PyInstaller spec for the Windows release: one folder, two executables sharing it.
# Build from the repository root:  pyinstaller packaging/windows/poefire.spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parents[1]  # noqa: F821 (SPECPATH is set by PyInstaller)

a = Analysis(  # noqa: F821
    [str(root / "packaging" / "windows" / "launcher.py")],
    pathex=[str(root / "src")],
    datas=[(str(root / "src" / "poefire" / "web" / "static"), "poefire/web/static")],
    # uvicorn picks its protocol/loop implementations by import string at runtime
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("poefire"),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)  # noqa: F821

icon = str(root / "src" / "poefire" / "web" / "static" / "favicon.ico")  # Ember Gem, from logo.svg
common = dict(exclude_binaries=True, debug=False, strip=False, upx=False, icon=icon)
cli = EXE(pyz, a.scripts, [], name="poefire", console=True, **common)  # noqa: F821
gui = EXE(pyz, a.scripts, [], name="poefire-ui", console=False, **common)  # noqa: F821

COLLECT(cli, gui, a.binaries, a.datas, name="poefire", strip=False, upx=False)  # noqa: F821
