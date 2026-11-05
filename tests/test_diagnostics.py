"""Logging and the debug report (Settings > Diagnostics)."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_campaign import H, Log, env  # noqa: F401  (env is a fixture)

from poefire import diagnostics, logs
from poefire.service import TrackerService
from poefire.store import Store
from poefire.web.runtime import AppRuntime


def test_log_file_and_verbose(tmp_path: Path) -> None:
    try:
        path = logs.setup(tmp_path)
        assert path == tmp_path / "logs" / "poefire.log"
        log = logging.getLogger("poefire.test")
        log.debug("hidden")
        log.info("shown %d", 1)
        logs.set_verbose(True)
        log.debug("now shown")
        text = path.read_text()
        assert "shown 1" in text and "now shown" in text and "hidden" not in text
        assert "INFO    [MainThread] poefire.test: shown 1" in text
    finally:
        logs.teardown()


def test_debug_report(env: tuple[TestClient, AppRuntime, Path], tmp_path: Path) -> None:  # noqa: F811
    client, rt, path = env
    try:
        logs.setup(rt.cfg.db_path.parent)
        client.post("/api/campaign/start", json={}, headers=H)
        log = Log()
        log.wait(2)
        log.enter("1_1_1", "The Twilight Strand")
        log.level(2, "Witch", "Hexy")
        log.line("@From Someone: wtb your stuff")  # a whisper: never in the report
        log.line("#Global: hello")  # chat
        log.line(": Stranger has joined the area.")  # another player's name
        path.write_text("\n".join(log.lines) + "\n")
        rt.service.poll()  # type: ignore[union-attr]

        r = client.post("/api/diagnostics/report", headers=H).json()
        assert r["name"].startswith("poefire-report-") and Path(r["path"]).is_file()
        zipped = client.get(f"/api/diagnostics/report/{r['name']}")
        assert zipped.headers["content-type"] == "application/zip"
        z = zipfile.ZipFile(io.BytesIO(zipped.content))
        names = set(z.namelist())
        assert {"report.txt", "client-log.txt", "campaign.json", "logs/poefire.log"} <= names
        report = z.read("report.txt").decode()
        assert "reading: yes" in report and "line handler errors: 0" in report
        assert "character Hexy (Witch), act 1" in report and "POESESSID" not in report
        client_log = z.read("client-log.txt").decode()
        assert "You have entered The Twilight Strand." in client_log
        assert "Hexy (Witch) is now level 2" in client_log
        assert "wtb" not in client_log and "hello" not in client_log
        assert "Stranger" not in client_log
        campaign = json.loads(z.read("campaign.json"))
        assert campaign["active"]["character"] == "Hexy" and campaign["active"]["splits"]
        assert "campaign run 1 started" in z.read("logs/poefire.log").decode()
        assert "Hexy (Witch) level 2" in z.read("logs/poefire.log").decode()

        bad = client.get("/api/diagnostics/report/..%2Fconfig.toml")
        assert bad.status_code == 404
        reveal = client.post("/api/diagnostics/reveal", json={"what": "logs"}, headers=H).json()
        assert reveal["path"].endswith("logs")  # opened in Explorer on Windows only
    finally:
        logs.teardown()
        rt.stop()


def test_report_spots_a_non_english_client(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text(
        '2026/11/05 10:00:00 1 a [DEBUG Client 1] Generating level 1 area "1_1_1" with seed 1\n'
        "2026/11/05 10:00:02 2 a [INFO Client 1] : Du hast Die Küste des Zwielichts betreten.\n"
        "2026/11/05 10:01:00 3 a [INFO Client 1] : Hexy (Hexe) ist jetzt Stufe 2\n",
        encoding="utf-8",
    )
    lines, info = diagnostics.client_lines(log)
    assert len(lines) == 3 and info["counts"] == {"AreaGenerated": 1}
    assert any("not set to English" in h for h in info["hints"])
    assert info["unrecognised_system_messages"][0].startswith("Du hast")
    assert diagnostics.client_lines(tmp_path / "missing.txt")[1]["error"]


def test_a_failing_line_handler_does_not_stall_the_tail(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "Client.txt"
    log = Log()
    log.enter("1_1_1", "The Twilight Strand")
    path.write_text("\n".join(log.lines) + "\n")
    svc = TrackerService(Store(tmp_path / "db.sqlite3"), path)
    seen: list[str] = []

    def broken(event: object, line: str) -> None:
        raise ValueError("boom")

    svc.line_observers += [broken, lambda event, line: seen.append(line)]
    svc.start(from_beginning=True)
    with caplog.at_level(logging.ERROR, logger="poefire.service"):
        svc.poll()
    assert len(seen) == 3 and svc.observer_errors == 3  # every line still reached the others
    assert svc.tailer is not None and svc.tailer.offset == path.stat().st_size  # moved on
    assert "broken failed on line" in caplog.text
