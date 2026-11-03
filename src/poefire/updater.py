"""Self-update from GitHub releases (Windows build only).

1. `check()` asks the GitHub API for the newest release (public only, unless the beta channel
   is on) and compares it with the running version.
2. `stage()` downloads the release zip, verifies it against its .sha256 asset, and unpacks it
   into a staging folder (refusing any path that would land outside it).
3. `start_install()` starts the *new* version from the staging folder with the hidden
   `apply-update` command (`apply_update()`): it waits for this process to exit (ending it if
   it hangs), copies itself over the install folder, and restarts the app, logging every step
   to update.log. A running .exe can't overwrite itself on Windows, hence the second process.
   If the staged exe can't be started, a batch script (`launch_swap()`) does the same.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

REPO = "trigun0x2/poefire"
API = f"https://api.github.com/repos/{REPO}/releases"
ASSET_RE = re.compile(r"^poefire-(?P<version>[^/]+)-windows-x64\.zip$")
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-dev\.(\d+))?$")

type Version = tuple[int, int, int, int, int]


class UpdateError(Exception):
    pass


def parse_version(text: str) -> Version | None:
    """ "0.6.0" -> (0, 6, 0, 1, 0); "0.6.0-dev.12" -> (0, 6, 0, 0, 12). A release sorts after
    its own dev builds. None for anything else."""
    m = _VERSION_RE.match(text.strip())
    if m is None:
        return None
    major, minor, patch, dev = m.groups()
    return (int(major), int(minor), int(patch), 0 if dev else 1, int(dev or 0))


def is_newer(candidate: str, current: str) -> bool:
    new, cur = parse_version(candidate), parse_version(current)
    return new is not None and (cur is None or new > cur)


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    tag: str
    notes: str
    page_url: str
    zip_url: str
    sha256_url: str | None
    size: int
    prerelease: bool
    published_at: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version, "tag": self.tag, "notes": self.notes,
            "page_url": self.page_url, "size": self.size, "prerelease": self.prerelease,
            "published_at": self.published_at,
        }  # fmt: skip


def _release_info(release: dict[str, Any]) -> UpdateInfo | None:
    assets = {a.get("name", ""): a for a in release.get("assets") or ()}
    for name, asset in assets.items():
        m = ASSET_RE.match(name)
        if m is None:
            continue
        sha = assets.get(f"{name}.sha256")
        return UpdateInfo(
            version=m["version"],
            tag=release.get("tag_name", ""),
            notes=(release.get("body") or "").strip(),
            page_url=release.get("html_url", ""),
            zip_url=asset["browser_download_url"],
            sha256_url=sha["browser_download_url"] if sha else None,
            size=int(asset.get("size") or 0),
            prerelease=bool(release.get("prerelease")),
            published_at=release.get("published_at"),
        )
    return None


class Updater:
    def __init__(self, current: str, user_agent: str, http: httpx.Client | None = None) -> None:
        self.current = current
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.headers = {"User-Agent": user_agent, "Accept": "application/vnd.github+json"}

    def check(self, include_prereleases: bool = False) -> UpdateInfo | None:
        """The newest release newer than the running version, or None."""
        try:
            r = self.http.get(API, params={"per_page": 20}, headers=self.headers)
        except httpx.HTTPError as e:
            raise UpdateError(f"couldn't reach GitHub: {e}") from e
        if r.status_code != 200:
            raise UpdateError(f"GitHub answered HTTP {r.status_code}")
        best: UpdateInfo | None = None
        for release in r.json():
            if release.get("draft") or (release.get("prerelease") and not include_prereleases):
                continue
            info = _release_info(release)
            if info is None or parse_version(info.version) is None:
                continue
            if best is None or is_newer(info.version, best.version):
                best = info
        if best is not None and is_newer(best.version, self.current):
            return best
        return None

    def stage(
        self,
        info: UpdateInfo,
        root: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download, verify and unpack the release into root/<version>; returns that folder."""
        root.mkdir(parents=True, exist_ok=True)
        archive = root / f"poefire-{info.version}.zip"
        digest = hashlib.sha256()
        try:
            with self.http.stream("GET", info.zip_url, headers=self.headers) as r:
                if r.status_code != 200:
                    raise UpdateError(f"download failed: HTTP {r.status_code}")
                total = int(r.headers.get("content-length") or info.size or 0)
                done = 0
                with open(archive, "wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                        digest.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            if info.sha256_url:
                r2 = self.http.get(info.sha256_url, headers=self.headers)
                expected = r2.text.split()[0].lower() if r2.status_code == 200 and r2.text else ""
                if expected != digest.hexdigest():
                    raise UpdateError("the download doesn't match its published checksum")
        except httpx.HTTPError as e:
            raise UpdateError(f"download failed: {e}") from e

        target = root / info.version
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        with zipfile.ZipFile(archive) as z:
            base = target.resolve()
            for member in z.infolist():
                dest = (target / member.filename).resolve()
                if dest != base and base not in dest.parents:
                    raise UpdateError(f"unsafe path in update archive: {member.filename}")
            z.extractall(target)
        archive.unlink(missing_ok=True)
        if not (target / "poefire-ui.exe").exists():
            raise UpdateError("the update archive doesn't contain poefire-ui.exe")
        return target


def staging_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    return (Path(base) if base else Path(tempfile.gettempdir())) / "poefire" / "updates"


def install_dir() -> Path | None:
    """Folder of the running packaged app; None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def can_self_update() -> tuple[bool, str | None]:
    """Whether this process can replace itself, and why not."""
    if sys.platform != "win32":
        return False, "self-update is only available in the Windows app"
    folder = install_dir()
    if folder is None:
        return False, "running from source: update with git pull / uv sync instead"
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".poefire-write-test-"):
            pass
    except OSError:
        return False, (f"no permission to write to {folder}; move poefire to a folder you own "
                       "(e.g. under your user folder) or update manually")  # fmt: skip
    return True, None


SWAP_SCRIPT = r"""@echo off
rem poefire updater: wait for the app to exit, copy the new version in, restart it.
setlocal
set "PID={pid}"
set "SRC={src}"
set "DST={dst}"
set /a TRIES=0
echo %DATE% %TIME% batch updater: waiting for %PID% >> "{log}"
:wait
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
  set /a TRIES+=1
  if %TRIES% GEQ 60 taskkill /F /PID %PID% >nul 2>&1
  rem ping, not timeout: timeout needs a console
  ping -n 2 127.0.0.1 >nul
  goto wait
)
:copy
robocopy "%SRC%" "%DST%" /E /R:30 /W:1 /NFL /NDL /NJH /NJS /NP >> "{log}" 2>&1
if %ERRORLEVEL% GEQ 8 (
  echo robocopy failed with %ERRORLEVEL% >> "{log}"
) else (
  rmdir /S /Q "%SRC%"
)
start "" "%DST%\poefire-ui.exe"
(goto) 2>nul & del "%~f0"
"""


def launch_swap(staged: Path, target: Path, log: Path) -> Path:
    """Start the detached script that installs `staged` over `target` once we exit."""
    script = staged.parent / "apply-update.cmd"
    script.write_text(
        SWAP_SCRIPT.format(pid=os.getpid(), src=staged, dst=target, log=log), encoding="utf-8"
    )
    # a hidden console (not DETACHED_PROCESS): batch commands need one
    _spawn(["cmd.exe", "/c", str(script)], NEW_PROCESS_GROUP | NO_WINDOW, staged.parent)
    return script


DETACHED = 0x00000008
NEW_PROCESS_GROUP = 0x00000200
BREAKAWAY_FROM_JOB = 0x01000000  # survive the parent's job object, when it allows that
NO_WINDOW = 0x08000000


def _spawn(args: list[str], flags: int, cwd: Path) -> None:
    """Start a process that outlives us: outside our job object when allowed."""
    try:
        subprocess.Popen(args, creationflags=flags | BREAKAWAY_FROM_JOB, close_fds=True, cwd=cwd)
    except OSError:  # the job doesn't allow breakaway
        subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=cwd)


def launch_apply(staged: Path, target: Path, log: Path) -> None:
    """Start the staged new version to install itself over `target` once we exit."""
    exe = staged / "poefire-ui.exe"
    args = [str(exe), "apply-update", "--source", str(staged), "--target", str(target),
            "--pid", str(os.getpid()), "--log", str(log)]  # fmt: skip
    _spawn(args, DETACHED | NEW_PROCESS_GROUP, staged)


def start_install(staged: Path, target: Path, log: Path) -> str:
    """Hand the install over to another process; returns how ("app" or "script")."""
    try:
        launch_apply(staged, target, log)
        return "app"
    except OSError as e:
        _log(log, f"could not start {staged / 'poefire-ui.exe'} ({e}); using the batch updater")
        launch_swap(staged, target, log)
        return "script"


# --- the apply-update command (runs from the staging folder) ---


def _log(log: Path, msg: str) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED
        if not handle:
            return False
        try:
            return bool(k32.WaitForSingleObject(handle, 0) == 0x102)  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            k32.TerminateProcess(handle, 1)
            k32.CloseHandle(handle)
        return
    import signal

    os.kill(pid, signal.SIGTERM)


def apply_update(
    source: Path,
    target: Path,
    pid: int,
    log: Path,
    *,
    wait: float = 60,
    retries: int = 30,
    sleep: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] = pid_alive,
    kill: Callable[[int], None] = kill_pid,
    restart: Callable[[Path], None] | None = None,
) -> int:
    """Copy `source` over `target` once process `pid` has exited, then restart the app."""
    _log(log, f"applying update: {source} -> {target} (waiting for process {pid})")
    waited = 0.0
    while alive(pid):
        if waited >= wait:
            _log(log, f"process {pid} still running after {wait:.0f}s; ending it")
            kill(pid)
            sleep(2)
            break
        sleep(0.5)
        waited += 0.5
    failed: list[str] = []
    files = [p for p in source.rglob("*") if p.is_file()]
    for f in files:
        dest = target / f.relative_to(source)
        for attempt in range(retries):
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                break
            except OSError as e:
                if attempt == retries - 1:
                    failed.append(f"{dest}: {e}")
                else:
                    sleep(1)
    if failed:
        _log(log, f"update failed, {len(failed)} of {len(files)} files not replaced:")
        for line in failed[:20]:
            _log(log, f"  {line}")
    else:
        _log(log, f"update applied: {len(files)} files")
    (restart or _restart)(target)
    return 1 if failed else 0


def _restart(target: Path) -> None:
    exe = target / "poefire-ui.exe"
    _spawn([str(exe)], DETACHED | NEW_PROCESS_GROUP, target)


def cleanup_staging(root: Path, keep: Path | None = None) -> None:
    """Remove staged versions left after an update (the running app may be one of them)."""
    if not root.is_dir():
        return
    for d in root.iterdir():
        if d.is_dir() and (keep is None or d.resolve() != keep.resolve()):
            shutil.rmtree(d, ignore_errors=True)
