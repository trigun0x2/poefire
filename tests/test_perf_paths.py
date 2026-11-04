"""The batched queries and caches added for speed give the same answers as before."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from poefire.events import ZoneEntered
from poefire.loot.diff import ItemDelta
from poefire.loot.items import ItemEntry
from poefire.loot.snapshot import Snapshot
from poefire.pricing.book import PriceBook
from poefire.pricing.ninja import PriceLine
from poefire.store import Store
from poefire.tracker import Run, RunStatus, Segment
from poefire.web import overview, sessions, views
from poefire.web.cache import BookCache

T = datetime(2026, 11, 1, 12)


def book(chaos: float = 2.0) -> PriceBook:
    return PriceBook([("exchange", "Currency", [PriceLine("Orb", chaos)])])


def orb(n: int, sample_extra: dict[str, Any] | None = None) -> ItemDelta:
    return ItemDelta(
        "5||Orb", "Orb", 5, n, {"typeLine": "Orb", "frameType": 5, **(sample_extra or {})}
    )


def run(rid: str, start: datetime, status: RunStatus = RunStatus.FINISHED) -> Run:
    end = start + timedelta(minutes=5)
    done = status is RunStatus.FINISHED
    return Run(rid, "MapWorldsStrand", "Strand", 83, 1, start, status,
               segments=[Segment(start, start + timedelta(minutes=2)),
                         Segment(start + timedelta(minutes=3), end if done else None)],
               ended_at=end if done else None)  # fmt: skip


def add_loot(s: Store, rid: str, deltas: list[ItemDelta]) -> None:
    a = s.add_snapshot(Snapshot(T), "run_start", rid)
    b = s.add_snapshot(Snapshot(T), "run_return", rid)
    s.add_loot(rid, "run", a, b, deltas)


def test_batched_loot_matches_per_run_semantics(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as s:
        for i in range(620):  # more than one IN (...) chunk
            s.upsert_run(run(f"r{i}", T + timedelta(minutes=10 * i)))
        add_loot(s, "r0", [orb(3, {"icon": "old"})])
        add_loot(s, "r0", [orb(2, {"icon": "new"}),
                           ItemDelta("5||Gone", "Gone", 5, 1, {"typeLine": "Gone"})])  # fmt: skip
        add_loot(s, "r0", [ItemDelta("5||Gone", "Gone", 5, -1, {"typeLine": "Gone"})])
        add_loot(s, "r619", [orb(7)])
        loot = s.runs_loot([f"r{i}" for i in range(620)])
        assert set(loot) == {"r0", "r619"}
        (d,) = loot["r0"]  # "Gone" nets to zero and is left out
        assert d.quantity == 5  # summed over the run's hideout returns
        # one sample per kind of item (its newest row); a single run gets its own newest
        assert s.run_loot("r0")[0].sample["icon"] == "new"
        assert s.run_loot("r619")[0].quantity == 7
        runs = s.list_runs(limit=1000)
        assert len(runs) == 620 and all(len(r.segments) == 2 for r in runs)
        assert runs[0].segments[1].end is not None
        assert s.runs_with_loot_data([r.id for r in runs]) == {"r0", "r619"}


def test_book_cache_starts_over_with_new_prices() -> None:
    cache, b1, b2 = BookCache(), book(), book()
    cache.put(b1, "k", 1)
    assert cache.get(b1, "k") == 1
    assert cache.get(b2, "k") is None  # new prices: everything recomputed
    cache.put(b2, "k", 2)
    cache.clear()
    assert cache.get(b2, "k") is None


def test_run_values_are_cached_once_settled(tmp_path: Path) -> None:
    now = T + timedelta(hours=1)
    with Store(tmp_path / "db") as s:
        s.upsert_run(run("old", T))
        s.upsert_run(run("fresh", now - timedelta(minutes=6)))  # ended 1 min ago
        s.upsert_run(run("live", now - timedelta(minutes=1), RunStatus.ACTIVE))
        for rid in ("old", "fresh", "live"):
            add_loot(s, rid, [orb(1)])
        cache, b = BookCache(), book()
        runs = [s.get_run(r) for r in ("old", "fresh", "live")]
        first = views.run_rows(s, b, runs, now, cache)  # type: ignore[arg-type]
        assert [r.value.chaos for r in first] == [2.0, 2.0, 2.0]
        for rid in ("old", "fresh", "live"):  # more loot arrives for every run
            add_loot(s, rid, [orb(1)])
        again = views.run_rows(s, b, runs, now, cache)  # type: ignore[arg-type]
        # only the settled run comes from the cache; the others pick up the new loot
        assert [r.value.chaos for r in again] == [2.0, 4.0, 4.0]
        fresh_prices = views.run_rows(s, book(), runs, now, cache)  # type: ignore[arg-type]
        assert fresh_prices[0].value.chaos == 4.0


def test_past_session_summaries_are_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = T + timedelta(days=2)
    with Store(tmp_path / "db") as s:
        s.upsert_run(run("a", T))
        add_loot(s, "a", [orb(5)])
        s.add_session(T + timedelta(days=1))
        cache, b = BookCache(), book()
        first = sessions.history(s, b, now, cache)
        assert [(h["current"], h["maps"], h["chaos"]) for h in first] == [
            (True, 0, None),
            (False, 1, 10.0),
        ]
        calls: list[Any] = []
        real = sessions.runs_of

        def counting(st: Store, sp: sessions.Span) -> list[Run]:
            calls.append(sp)
            return real(st, sp)

        monkeypatch.setattr(sessions, "runs_of", counting)
        assert sessions.history(s, b, now, cache) == first
        assert all(sp.current for sp in calls)  # past sessions came from the cache
        assert sessions.history(s, book(3.0), now, cache)[1]["chaos"] == 15.0  # new prices


def test_thin_keeps_extremes_and_ends() -> None:
    pts = [(T + timedelta(minutes=i), float(i % 97), None) for i in range(5000)]
    pts[2500] = (pts[2500][0], 1000.0, None)  # a spike
    out = overview.thin(pts, 800)
    assert len(out) <= 802 and out[0] == pts[0] and out[-1] == pts[-1]
    assert any(p[1] == 1000.0 for p in out)
    assert [p[0] for p in out] == sorted(p[0] for p in out)
    assert overview.thin(pts[:10], 800) == pts[:10]


def test_holdings_are_cached_per_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with Store(tmp_path / "db") as s:
        item = ItemEntry.from_api(
            {"id": "1", "typeLine": "Orb", "frameType": 5, "stackSize": 4}, "t1"
        )
        s.add_snapshot(Snapshot(T, [item]), "manual", None)
        cache, b = BookCache(), book()
        first = overview.holdings(s, b, cache=cache)
        assert first["total_chaos"] == 8.0
        monkeypatch.setattr(s, "latest_snapshot_items", lambda: pytest.fail("not cached"))
        assert overview.holdings(s, b, cache=cache) == first
        assert overview.holdings(s, b, ["t1"], cache=cache)["total_chaos"] == 8.0
        monkeypatch.undo()
        s.add_snapshot(Snapshot(T, [item, item]), "manual", None)  # a new snapshot
        assert overview.holdings(s, b, cache=cache)["total_chaos"] == 16.0


def test_prune_events(tmp_path: Path) -> None:
    with Store(tmp_path / "db") as s:
        s.add_event(ZoneEntered(T - timedelta(days=100), "Old"), None)
        s.add_event(ZoneEntered(T, "New"), None)
        assert s.prune_events(T - timedelta(days=90)) == 1
        assert [e["zone"] for e in s.list_events()] == ["New"]


def test_campaign_list_without_splits(tmp_path: Path) -> None:
    from poefire.campaign import CampaignRun, Split

    with Store(tmp_path / "db") as s:
        s.save_campaign_run(
            CampaignRun(started_at=T, notes="n", splits=[Split("zone", "Z", 1, T, 1.0, 1.0)])
        )
        (light,) = s.list_campaign_runs(splits=False)
        assert light.splits == [] and light.notes == "n"
        assert len(s.list_campaign_runs()[0].splits) == 1
