from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from poefire.config import Config
from poefire.loot.diff import ItemDelta
from poefire.loot.snapshot import Snapshot
from poefire.pricing.book import PriceBook
from poefire.pricing.ninja import NinjaClient, NinjaError, PriceLine
from poefire.pricing.service import PriceService
from poefire.store import Store
from poefire.tracker import Run, RunStatus, Segment
from poefire.web import views
from poefire.web.app import create_app
from poefire.web.runtime import AppRuntime

REAL_LOG = Path(__file__).parent / "data" / "real_client_2026-11-01.txt"
NOW = datetime(2026, 11, 1, 16, 7, 31)
HEADERS = {"X-Poefire": "1"}


class OfflineNinja(NinjaClient):
    def __init__(self) -> None:
        super().__init__("test")

    def fetch(self, source: str, league: str, type_: str) -> list[PriceLine]:
        raise NinjaError("offline")

    def leagues(self) -> list[str]:
        return ["Keepers", "Standard"]


@pytest.fixture
def app(tmp_path: Path) -> TestClient:
    db = tmp_path / "db.sqlite3"
    log = tmp_path / "Client.txt"
    lines = REAL_LOG.read_text().splitlines(keepends=True)
    cut = next(i for i, ln in enumerate(lines)
               if "16:06:59" in ln and "You have entered Spider Lair" in ln)  # fmt: skip
    log.write_text("".join(lines[: cut + 1]))
    with Store(db) as s, s.transaction():
        s.save_prices("Keepers", "exchange", "Currency", 1e12,
                      [PriceLine("Divine Orb", 200.0)])  # fmt: skip
    cfg = Config(db_path=db, league="Keepers", characters=["xyz_bow"])
    rt = AppRuntime(cfg, PriceService(OfflineNinja(), lambda: Store(db)), recorder=None,
                    log_path=log, now=lambda: NOW)  # fmt: skip
    assert rt.service is not None
    rt.service.start(from_beginning=True)
    rt.service.poll()
    # loot for the first finished run
    with Store(db) as s, s.transaction():
        run = next(r for r in s.list_runs() if r.map_name == "Mesa")
        a = s.add_snapshot(Snapshot(NOW), "run_start", run.id)
        b = s.add_snapshot(Snapshot(NOW), "run_return", run.id)
        s.add_loot(run.id, "run", a, b, [
            ItemDelta("k", "Divine Orb", 5, 1, {"typeLine": "Divine Orb", "frameType": 5}),
            ItemDelta("c", "Chaos Orb", 5, 20, {"typeLine": "Chaos Orb", "frameType": 5}),
        ])  # fmt: skip
    client = TestClient(create_app(rt))
    client.headers["host"] = "127.0.0.1:8460"
    return client


def test_status_shows_live_run(app: TestClient) -> None:
    s = app.get("/api/status").json()
    assert s["league"] == "Keepers"
    assert s["zone"] == {"name": "Spider Lair", "kind": "map"}
    run = s["run"]
    assert run["map_name"] == "Spider Lair" and run["status"] == "active"
    assert run["deaths"] == 2
    assert run["active_seconds"] == 10 + 70 + 32  # open segment counted up to "now"
    assert s["prices"]["chaos_per_divine"] == 200.0
    assert s["loot"]["enabled"] is False


def test_runs_and_session(app: TestClient) -> None:
    data = app.get("/api/runs").json()
    names = [r["map_name"] for r in data["runs"]]
    assert names == ["Spider Lair", "Mesa", "Fungal Hollow", "Cells", "Arachnid Nest"]
    mesa = data["runs"][1]
    assert mesa["loot_tracked"] and mesa["chaos"] == 220.0 and mesa["divine"] == 1.1
    assert mesa["chaos_per_hour"] == round(220 * 3600 / 115)
    assert data["runs"][2]["loot_tracked"] is False and data["runs"][2]["chaos"] is None
    s = data["session"]
    assert s["runs"] == 5 and s["deaths"] == 4 and s["loot_tracked_runs"] == 1
    assert s["chaos"] == 220.0 and s["best_run"]["map_name"] == "Mesa"


def test_run_detail(app: TestClient) -> None:
    mesa = next(r for r in app.get("/api/runs").json()["runs"] if r["map_name"] == "Mesa")
    d = app.get(f"/api/runs/{mesa['id']}").json()
    assert [(i["name"], i["chaos_total"]) for i in d["loot"]["items"]] == [
        ("Divine Orb", 200.0), ("Chaos Orb", 20.0)]  # fmt: skip
    assert d["before"]["items"] == []
    assert app.get("/api/runs/nope").status_code == 404


def test_league_switch(app: TestClient) -> None:
    assert app.get("/api/leagues").json()["options"][:2] == ["Keepers", "Standard"]
    assert app.post("/api/league", json={"league": "Standard"}, headers=HEADERS).json() == {
        "current": "Standard"
    }
    assert app.get("/api/status").json()["league"] == "Standard"


def test_snapshot_without_loot_tracking_is_409(app: TestClient) -> None:
    assert app.post("/api/snapshot", headers=HEADERS).status_code == 409


def test_post_requires_custom_header(app: TestClient) -> None:
    r = app.post("/api/league", json={"league": "Standard"})
    assert r.status_code == 403
    assert app.get("/api/status").json()["league"] == "Keepers"


def test_foreign_host_rejected(app: TestClient) -> None:
    assert app.get("/api/status", headers={"host": "evil.example:8460"}).status_code == 403


def test_index_served(app: TestClient) -> None:
    r = app.get("/")
    assert r.status_code == 200 and "poefire" in r.text
    assert app.get("/static/app.js").status_code == 200


# --- views ---


def mkrun(rid: str, start: datetime, minutes: int) -> Run:
    end = start + timedelta(minutes=minutes)
    return Run(rid, "MapWorldsStrand", "Strand", 83, 1, start, RunStatus.FINISHED,
               segments=[Segment(start, end)], ended_at=end)  # fmt: skip


def test_current_session_splits_on_gap(tmp_path: Path) -> None:
    t = datetime(2026, 11, 1, 12)
    runs = [mkrun("c", t + timedelta(hours=3, minutes=10), 5),  # newest first
            mkrun("b", t + timedelta(hours=3), 5),
            mkrun("a", t, 5)]  # fmt: skip
    with Store(tmp_path / "db") as s:
        rows = views.run_rows(s, PriceBook([]), runs, t)
    session = views.current_session(rows, timedelta(minutes=30))
    assert [r.run.id for r in session] == ["c", "b"]
    summary = views.session_summary(session, None, t + timedelta(hours=4))
    assert summary["runs"] == 2 and summary["active_seconds"] == 600
    assert summary["wall_seconds"] == 15 * 60
    assert summary["chaos"] is None  # no loot data, not zero
