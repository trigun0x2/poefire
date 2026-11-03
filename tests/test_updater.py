from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from poefire import updater
from poefire.config import Config
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.updater import UpdateError, Updater, is_newer, parse_version
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime


@pytest.mark.parametrize(
    ("a", "b", "newer"),
    [
        ("0.6.0", "0.5.0", True),
        ("v0.10.0", "0.9.0", True),
        ("0.6.0", "0.6.0-dev.12", True),  # the release beats its dev builds
        ("0.6.0-dev.13", "0.6.0-dev.12", True),
        ("0.6.0-dev.12", "0.5.0", True),
        ("0.5.0", "0.5.0", False),
        ("0.4.9", "0.5.0", False),
        ("garbage", "0.5.0", False),
        ("0.5.0", "not-a-version", True),
    ],
)
def test_version_order(a: str, b: str, newer: bool) -> None:
    assert is_newer(a, b) is newer


def test_parse_version() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3, 1, 0)
    assert parse_version("1.2.3-dev.7") == (1, 2, 3, 0, 7)
    assert parse_version("1.2") is None


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


GOOD_ZIP = make_zip({"poefire-ui.exe": b"MZ new ui", "poefire.exe": b"MZ new cli",
                     "_internal/base_library.zip": b"x"})  # fmt: skip


def release(version: str, prerelease: bool = False, draft: bool = False,
            with_asset: bool = True) -> dict[str, Any]:  # fmt: skip
    name = f"poefire-{version}-windows-x64.zip"
    assets = [
        {"name": name, "size": len(GOOD_ZIP), "browser_download_url": f"https://dl/{name}"},
        {"name": f"{name}.sha256", "size": 90, "browser_download_url": f"https://dl/{name}.sha256"},
    ] if with_asset else []  # fmt: skip
    return {"tag_name": f"v{version}", "body": f"notes for {version}", "prerelease": prerelease,
            "draft": draft, "html_url": f"https://gh/releases/v{version}", "assets": assets,
            "published_at": "2026-11-03T18:00:00Z"}  # fmt: skip


class GitHub:
    def __init__(self, releases: list[dict[str, Any]], zip_bytes: bytes = GOOD_ZIP,
                 sha: str | None = None) -> None:  # fmt: skip
        self.releases = releases
        self.zip = zip_bytes
        self.sha = sha if sha is not None else hashlib.sha256(zip_bytes).hexdigest()
        self.requests: list[httpx.Request] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        if r.url.host == "api.github.com":
            return httpx.Response(200, json=self.releases)
        if r.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{self.sha}  {r.url.path.rsplit('/', 1)[1]}\n")
        return httpx.Response(200, content=self.zip)


def make_updater(gh: GitHub, current: str = "0.5.0") -> Updater:
    return Updater(current, "poefire/test", http=httpx.Client(transport=httpx.MockTransport(gh)))


def test_check_picks_newest_public_release() -> None:
    gh = GitHub([release("0.7.0-dev.3", prerelease=True), release("0.6.0"),
                 release("0.8.0", draft=True), release("0.9.0", with_asset=False)])  # fmt: skip
    info = make_updater(gh).check()
    assert info is not None and info.version == "0.6.0" and not info.prerelease
    assert info.notes == "notes for 0.6.0"
    assert gh.requests[0].headers["User-Agent"] == "poefire/test"
    beta = make_updater(gh).check(include_prereleases=True)
    assert beta is not None and beta.version == "0.7.0-dev.3"
    assert make_updater(gh, current="0.6.0").check() is None


def test_check_errors() -> None:
    def fail(r: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"})

    u = Updater("0.5.0", "t", http=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(UpdateError, match="403"):
        u.check()


def test_stage_downloads_verifies_and_unpacks(tmp_path: Path) -> None:
    gh = GitHub([release("0.6.0")])
    u = make_updater(gh)
    info = u.check()
    assert info is not None
    progress: list[tuple[int, int]] = []
    staged = u.stage(info, tmp_path, progress=lambda d, t: progress.append((d, t)))
    assert staged == tmp_path / "0.6.0"
    assert (staged / "poefire-ui.exe").read_bytes() == b"MZ new ui"
    assert (staged / "_internal" / "base_library.zip").exists()
    assert not list(tmp_path.glob("*.zip"))  # archive removed after unpacking
    assert progress[-1][0] == len(GOOD_ZIP)


def test_stage_rejects_bad_checksum(tmp_path: Path) -> None:
    u = make_updater(GitHub([release("0.6.0")], sha="0" * 64))
    info = u.check()
    assert info is not None
    with pytest.raises(UpdateError, match="checksum"):
        u.stage(info, tmp_path)
    assert not (tmp_path / "0.6.0").exists()


def test_stage_rejects_path_traversal(tmp_path: Path) -> None:
    evil = make_zip({"poefire-ui.exe": b"x", "../../evil.txt": b"pwned"})
    u = make_updater(GitHub([release("0.6.0")], zip_bytes=evil))
    info = u.check()
    assert info is not None
    with pytest.raises(UpdateError, match="unsafe path"):
        u.stage(info, tmp_path / "updates")
    assert not (tmp_path / "evil.txt").exists()


def test_stage_requires_the_app(tmp_path: Path) -> None:
    u = make_updater(GitHub([release("0.6.0")], zip_bytes=make_zip({"readme.txt": b"hi"})))
    info = u.check()
    assert info is not None
    with pytest.raises(UpdateError, match=r"poefire-ui\.exe"):
        u.stage(info, tmp_path)


def test_cannot_self_update_from_source() -> None:
    ok, why = updater.can_self_update()
    assert ok is False and why


def test_swap_script_waits_copies_and_restarts(tmp_path: Path) -> None:
    script = updater.SWAP_SCRIPT.format(pid=1234, src=tmp_path / "s", dst=tmp_path / "d",
                                        log=tmp_path / "u.log")  # fmt: skip
    assert 'tasklist /FI "PID eq %PID%"' in script and 'set "PID=1234"' in script
    assert "robocopy" in script and "poefire-ui.exe" in script


# --- runtime + API ---


class OfflineNinja(NinjaClient):
    def __init__(self) -> None:
        super().__init__("test")

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        return []


@pytest.fixture
def app(tmp_path: Path) -> tuple[TestClient, AppRuntime]:
    db = tmp_path / "db.sqlite3"
    rt = AppRuntime(
        Config(db_path=db, league="Keepers"), PriceService(OfflineNinja(), lambda: Store(db)),
        recorder=None, log_path=tmp_path / "none.txt",
        updater=make_updater(GitHub([release("0.6.0"), release("0.7.0-dev.1", prerelease=True)]),
                             current="0.5.0"),
    )  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    return client, rt


H = {"X-Poefire": "1"}


def test_update_check_api_and_channel(app: tuple[TestClient, AppRuntime]) -> None:
    client, rt = app
    assert client.get("/api/update").json()["available"] is None
    u = client.post("/api/update/check", headers=H).json()
    assert u["available"]["version"] == "0.6.0" and u["checked_at"]
    assert client.get("/api/status").json()["update"]["available"]["version"] == "0.6.0"
    rt.cfg.update_channel = "beta"
    assert (
        client.post("/api/update/check", headers=H).json()["available"]["version"] == "0.7.0-dev.1"
    )


def test_install_refused_when_running_from_source(app: tuple[TestClient, AppRuntime]) -> None:
    client, _ = app
    assert client.post("/api/update/install", headers=H).status_code == 409  # nothing available
    client.post("/api/update/check", headers=H)
    r = client.post("/api/update/install", headers=H)
    assert r.status_code == 409 and r.json()["detail"]  # "from source" / "Windows only"


def test_install_flow_stages_swaps_and_exits(
    app: tuple[TestClient, AppRuntime], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, rt = app
    client.post("/api/update/check", headers=H)
    rt.update.can_install = True
    installed = tmp_path / "app"
    installed.mkdir()
    swaps: list[tuple[Path, Path]] = []
    exited = threading.Event()
    monkeypatch.setattr("poefire.web.runtime.install_dir", lambda: installed)
    monkeypatch.setattr("poefire.web.runtime.staging_root", lambda: tmp_path / "updates")
    monkeypatch.setattr("poefire.web.runtime.launch_swap",
                        lambda staged, target, log: swaps.append((staged, target)))  # fmt: skip
    rt.request_exit = exited.set
    assert client.post("/api/update/install", headers=H).json()["stage"] == "downloading"
    assert exited.wait(5)
    assert swaps == [(tmp_path / "updates" / "0.6.0", installed)]
    assert rt.update.stage == "installing"
