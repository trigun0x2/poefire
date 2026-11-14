"""The command line: the windowed exe's missing console, and a second launch."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_web import OfflineNinja

from poefire import cli
from poefire.config import Config
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

UVICORN_LOGGING = (
    "import uvicorn; uvicorn.Config(lambda: None, log_level='warning'); "
    "print('ok', file=sys.__stdout__)"
)


def test_no_console_does_not_break_uvicorn() -> None:
    """poefire.exe has no console: sys.stdout is None. uvicorn's log setup used to crash on it
    ("Unable to configure formatter 'default'")."""
    broken = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.stdout = sys.stderr = None; {UVICORN_LOGGING}"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert broken.returncode != 0 and "ok" not in broken.stdout  # the traceback goes nowhere
    fixed = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stdout = sys.stderr = None; "
         f"from poefire.cli import _ensure_streams; _ensure_streams(); {UVICORN_LOGGING}"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert fixed.returncode == 0 and fixed.stdout.strip() == "ok"


def test_window_show_endpoint(tmp_path: Path) -> None:
    db = tmp_path / "db"
    rt = AppRuntime(Config(db_path=db, league="Keepers"),
                    PriceService(OfflineNinja(), lambda: Store(db)), recorder=None)  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    assert client.post("/api/window/show", headers={"X-Poefire": "1"}).json() == {"ok": False}
    shown: list[int] = []
    rt.request_show = lambda: shown.append(1)
    assert client.post("/api/window/show", headers={"X-Poefire": "1"}).json() == {"ok": True}
    assert client.post("/api/window/show").status_code == 403  # other websites can't
    assert shown == [1]


def test_a_second_launch_shows_the_running_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def get(url: str, **kw: Any) -> httpx.Response:
        calls.append(("GET", url))
        return httpx.Response(200, json={"loot": {}, "league": "Keepers"})

    def post(url: str, **kw: Any) -> httpx.Response:
        calls.append(("POST", url))
        assert kw["headers"] == {"X-Poefire": "1"}
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(cli, "AppRuntime", None, raising=False)  # must not start a second one
    args = cli.build_parser().parse_args(["ui", "--window", "--port", "8471"])
    assert cli.cmd_ui(args) == 0  # (not main(): that sets up logging for the whole process)
    assert calls == [("GET", "http://127.0.0.1:8471/api/status"),
                     ("POST", "http://127.0.0.1:8471/api/window/show")]  # fmt: skip


def test_nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(url: str, **kw: Any) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refused)
    assert cli._show_running("http://127.0.0.1:8471/") is False
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, text="<html>"))
    assert cli._show_running("http://127.0.0.1:8471/") is False  # something else on the port
