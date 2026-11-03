"""Self-update from GitHub releases (Windows build only).

1. `check()` asks the GitHub API for the newest release (public only, unless the beta channel
   is on) and compares it with the running version.
2. `stage()` downloads the release zip, verifies it against its .sha256 asset, and unpacks it
   into a staging folder (refusing any path that would land outside it).
3. `launch_swap()` writes a small batch script and starts it detached: it waits for this
   process to exit, copies the staged files over the install folder, and restarts the app.
   A running .exe can't overwrite itself on Windows, hence the separate script.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
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
:wait
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if not errorlevel 1 (
  set /a TRIES+=1
  if %TRIES% GEQ 120 goto copy
  timeout /t 1 /nobreak >nul
  goto wait
)
:copy
robocopy "%SRC%" "%DST%" /E /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >> "{log}" 2>&1
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
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(  # our own generated script
        ["cmd.exe", "/c", str(script)], creationflags=flags, close_fds=True, cwd=staged.parent
    )
    return script
