from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from test_web import HEADERS, app, mkrun  # noqa: F401  (app is a fixture)

from poefire.pricing.valuation import Valuation
from poefire.store import Store
from poefire.web import sessions, views


def test_first_start_opens_the_current_play_session(tmp_path: Path) -> None:
    t = datetime(2026, 11, 1, 12)
    with Store(tmp_path / "db") as s:
        for r in (mkrun("a", t, 5), mkrun("b", t + timedelta(hours=3), 5),
                  mkrun("c", t + timedelta(hours=3, minutes=10), 5)):  # fmt: skip
            s.upsert_run(r)
        sessions.ensure_session(s, timedelta(minutes=30))
        sessions.ensure_session(s, timedelta(minutes=30))  # only once
        assert [start for _, start in s.list_sessions()] == [t + timedelta(hours=3)]
        old, cur = sessions.spans(s)
        assert [r.id for r in sessions.runs_of(s, old)] == ["a"]
        assert [r.id for r in sessions.runs_of(s, cur)] == ["c", "b"]


def test_first_start_without_runs_keeps_everything_current(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as s:
        sessions.ensure_session(s, timedelta(minutes=30))
        assert s.list_sessions() == []
        s.upsert_run(mkrun("a", datetime(2026, 11, 1, 12), 5))
        sessions.ensure_session(s, timedelta(minutes=30))  # a later start doesn't regroup
        (span,) = sessions.spans(s)
        assert span.current and [r.id for r in sessions.runs_of(s, span)] == ["a"]


def test_session_history_new_session_and_delete(app: TestClient) -> None:  # noqa: F811
    (cur,) = app.get("/api/sessions").json()["sessions"]
    assert cur["current"] and cur["maps"] == 5 and cur["deaths"] == 4
    assert cur["loot_tracked_maps"] == 1 and cur["chaos"] == 220.0 and cur["divine"] == 1.1
    assert cur["avg_chaos"] == 220.0 and cur["avg_map_seconds"] > 0 and cur["in_progress"]

    new_id = app.post("/api/sessions/new", headers=HEADERS).json()["id"]
    fresh = app.get("/api/runs").json()
    # the map you're inside (Spider Lair) moves to the new session; nothing else does
    assert [r["map_name"] for r in fresh["runs"]] == ["Spider Lair"]
    assert fresh["last_map"] is None and fresh["recent"]["maps"] == 0
    assert fresh["session"]["id"] == new_id and fresh["session"]["maps"] == 1

    hist = app.get("/api/sessions").json()["sessions"]
    assert [(h["id"], h["current"], h["maps"]) for h in hist] == [(new_id, True, 1), (0, False, 4)]
    assert hist[1]["ended_at"] is not None and not hist[1]["in_progress"]
    old = app.get("/api/runs?session=0").json()
    assert len(old["runs"]) == 4 and old["session"]["current"] is False
    assert old["recent"]["of"] == 4  # a past session averages all its maps

    assert app.post(f"/api/sessions/{new_id}/delete", headers=HEADERS).status_code == 409
    assert app.post("/api/sessions/99/delete", headers=HEADERS).status_code == 404
    assert app.get("/api/runs?session=99").status_code == 404
    assert app.post("/api/sessions/0/delete", headers=HEADERS).json() == {"ok": True}
    assert [h["id"] for h in app.get("/api/sessions").json()["sessions"]] == [new_id]


def test_delete_a_finished_session(app: TestClient) -> None:  # noqa: F811
    first = app.post("/api/sessions/new", headers=HEADERS).json()["id"]
    app.post("/api/sessions/new", headers=HEADERS)
    # an empty past session is hidden from the history
    hist = app.get("/api/sessions").json()["sessions"]
    assert first not in [h["id"] for h in hist]
    assert app.post(f"/api/sessions/{first}/delete", headers=HEADERS).json() == {"ok": True}


def test_delete_runs_removes_their_loot(tmp_path: Path) -> None:
    from poefire.loot.diff import ItemDelta
    from poefire.loot.snapshot import Snapshot

    t = datetime(2026, 11, 1, 12)
    with Store(tmp_path / "db") as s:
        s.upsert_run(mkrun("a", t, 5))
        s.upsert_run(mkrun("b", t + timedelta(minutes=10), 5))
        a = s.add_snapshot(Snapshot(t), "run_start", "a")
        b = s.add_snapshot(Snapshot(t), "run_return", "a")
        s.add_loot("a", "run", a, b, [ItemDelta("c", "Chaos Orb", 5, 3, {})])
        s.delete_runs(["a"])
        assert [r.id for r in s.list_runs()] == ["b"] and s.run_loot("a") == []


def _row(rid: str, name: str, start: datetime, minutes: int, chaos: float | None) -> views.RunRow:
    r = mkrun(rid, start, minutes)
    r.map_name = name
    return views.RunRow(r, Valuation(chaos or 0.0), chaos is not None, minutes * 60.0)


def test_chaos_per_hour_counts_hideout_time() -> None:
    t = datetime(2026, 11, 1, 12)
    rows = [  # newest first: 10 min maps with 5 min in the hideout after each
        _row("c", "Strand", t + timedelta(minutes=30), 10, 100),
        _row("b", "Strand", t + timedelta(minutes=15), 10, 200),
        _row("a", "Dunes", t, 10, None),  # no loot data: counts with the average rate
    ]
    # 300c in 20 map minutes = 15c/min; 30 map minutes of 40 played: 15 * 60 * 30/40
    assert views.per_hour(rows, 100.0) == {"chaos_per_hour": 675, "divine_per_hour": 6.75}
    assert views.per_hour(rows[2:], None)["chaos_per_hour"] is None  # nothing priced

    late = [_row("d", "Dunes", t + timedelta(hours=2), 10, 0), *rows]  # a long break: capped
    assert views.per_hour(late, None)["chaos_per_hour"] == round(300 / 30 * 40 / 60 * 60)

    strand, dunes = views.map_stats(late, 100.0)
    assert strand == {"map_name": "Strand", "maps": 2, "loot_tracked_maps": 2,
                      "avg_chaos": 150.0, "avg_divine": 1.5, "avg_seconds": 600, "deaths": 0,
                      "chaos_per_hour": round(300 / 35 * 60), "best_chaos": 200.0}  # fmt: skip
    assert dunes["maps"] == 2 and dunes["loot_tracked_maps"] == 1 and dunes["avg_chaos"] == 0


def test_runs_and_history_include_rates(app: TestClient) -> None:  # noqa: F811
    data = app.get("/api/runs").json()
    assert "chaos_per_hour" in data["session"] and data["maps"]
    assert {m["map_name"] for m in data["maps"]} <= {r["map_name"] for r in data["runs"]}
    assert "chaos_per_hour" in app.get("/api/sessions").json()["sessions"][0]
