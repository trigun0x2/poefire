"""Trade sales taken out of the loot (the website's trade history; see poefire.loot.sales)."""

from __future__ import annotations

from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from test_loot import T, currency, make_recorder, snap

from poefire.loot import sales
from poefire.loot.recorder import SALES_KEY, Trigger
from poefire.loot.sales import Sale, SaleBook


def utc(minute: int) -> str:
    """T (local time) + minutes, written the way the site does: UTC."""
    return (T + timedelta(minutes=minute)).astimezone(UTC).isoformat()


def sale(item_id: str, minute: int, amount: Any, currency_id: str = "divine",
         name: str = "Bladefall of Impaling") -> dict[str, Any]:  # fmt: skip
    """Shaped like /api/trade/history/<league>."""
    return {"time": utc(minute), "item_id": item_id,
            "item": {"id": item_id, "name": "", "typeLine": name, "baseType": name,
                     "frameType": 4, "league": "Standard"},
            "price": {"amount": amount, "currency": currency_id}}  # fmt: skip


def test_parse_history() -> None:
    data = {"result": [
        sale("b", 5, 3, "chaos", "<<set:MS>>Some Map"), sale("a", 1, 44),
        sale("frac", 2, 1.5), sale("odd", 3, 2, "some-new-currency"), {"time": "nope"},
    ]}  # fmt: skip
    got = sales.parse_history(data)
    assert [(s.item_id, s.amount, s.currency, s.item_name) for s in got] == [
        ("a", 44, "Divine Orb", "Bladefall of Impaling"),
        ("b", 3, "Chaos Orb", "Some Map"),
    ]
    assert got[0].sold_at == T + timedelta(minutes=1)  # local time, like snapshots
    assert sales.parse_history({"error": {"message": "Invalid query"}}) == []
    assert Sale.from_json(got[0].to_json()) == got[0]


def test_first_fetch_is_only_remembered() -> None:
    book = SaleBook([], [])
    old = sales.parse_history({"result": [sale("old", -60, 10)]})
    book.add(old, first_fetch=True)
    assert book.pending == [] and book.seen == ["old"]
    book.add(old + sales.parse_history({"result": [sale("new", 1, 2)]}), first_fetch=False)
    assert [s.item_id for s in book.pending] == ["new"] and book.seen == ["old", "new"]
    assert SaleBook.from_json(book.to_json()) == book
    assert SaleBook.from_json(None) == SaleBook([], [])


def test_sales_are_taken_out_of_the_map_they_were_paid_into(tmp_path: Path) -> None:
    div, c = "Divine Orb", "Chaos Orb"
    history: list[dict[str, Any]] = [{"result": []}]
    rec, store = make_recorder(
        tmp_path,
        [
            snap(0, [currency(div, 1, "d"), currency(c, 10, "c")]),
            snap(1, [currency(div, 1, "d"), currency(c, 10, "c")]),  # map 1 starts
            # map 1: 44 div from the sale (at minute 3) + 1 div and 20c that dropped
            snap(5, [currency(div, 46, "d"), currency(c, 30, "c")]),
            snap(6, [currency(div, 46, "d"), currency(c, 30, "c")]),  # map 2 starts
            # map 2: the 5c sale was paid, the 7 div one (minute 7) not yet: nothing to take
            snap(10, [currency(div, 46, "d"), currency(c, 35, "c")]),
            snap(11, [currency(div, 46, "d"), currency(c, 35, "c")]),  # map 3 starts
            snap(15, [currency(div, 53, "d"), currency(c, 35, "c")]),  # map 3: the 7 div arrive
        ],
    )
    rec.sales = lambda: sales.parse_history(history[0])
    rec.monotonic = iter(range(0, 10_000, 100)).__next__  # every record may fetch
    rec.record(Trigger.MANUAL)  # first fetch: empty, remembered
    history[0] = {"result": [sale("gem", 3, 44), sale("map", 8, 5, "chaos"),
                             sale("ring", 7, 7)]}  # fmt: skip
    rec.record(Trigger.RUN_START, "r1")
    r1 = rec.record(Trigger.RUN_RETURN, "r1")
    assert {d.name: d.quantity for d in r1.deltas} == {
        div: 45,
        c: 20,
        "Sold on trade · Bladefall of Impaling": -44,
    }
    assert {d.name: d.quantity for d in store.run_loot("r1")} == {
        div: 45,
        c: 20,
        "Sold on trade · Bladefall of Impaling": -44,
    }  # net 1 div 20c
    rec.record(Trigger.RUN_START, "r2")
    r2 = rec.record(Trigger.RUN_RETURN, "r2")
    assert {d.name: d.quantity for d in r2.deltas} == {
        c: 5,
        "Sold on trade · Bladefall of Impaling": -5,
    }  # the "map" sale (same item name)
    rec.record(Trigger.RUN_START, "r3")
    r3 = rec.record(Trigger.RUN_RETURN, "r3")
    assert {d.name: d.quantity for d in r3.deltas} == {
        div: 7,
        "Sold on trade · Bladefall of Impaling": -7,
    }
    assert store.get_json(SALES_KEY)["pending"] == []  # every sale counted once


def test_unpaid_sales_expire_and_small_gains_stay_loot() -> None:
    s = Sale("x", T, 10, "Divine Orb", "Thing")
    book = SaleBook([s], ["x"])
    d = currency("Divine Orb", 3)
    from poefire.loot.diff import ItemDelta

    gain = [ItemDelta("5||Divine Orb", "Divine Orb", 5, 3, d)]
    assert book.offsets(gain, T + timedelta(hours=1)) == []  # 3 div isn't the 10 div sale
    assert book.pending == [s]
    assert book.offsets([], T + sales.KEEP_PENDING + timedelta(minutes=1)) == []
    assert book.pending == []  # gave up on it
    later = SaleBook([Sale("y", T + timedelta(hours=2), 1, "Divine Orb", "Y")], ["y"])
    assert later.offsets(gain, T + timedelta(hours=1)) == []  # sold after this snapshot
    assert len(later.pending) == 1


def test_a_failing_history_backs_off(tmp_path: Path) -> None:
    rec, _ = make_recorder(tmp_path, [snap(0, []), snap(1, []), snap(2, [])])
    calls: list[int] = []

    def broken() -> list[Sale]:
        calls.append(1)
        raise RuntimeError("HTTP 400 from /api/trade/history/X")

    clock = [0.0]
    rec.sales = broken
    rec.monotonic = lambda: clock[0]
    rec.record(Trigger.MANUAL)
    clock[0] += 120
    rec.record(Trigger.MANUAL)  # within the 30 min back-off: not asked again
    clock[0] += 1800
    rec.record(Trigger.MANUAL)
    assert len(calls) == 2


def test_session_api_trade_history_never_blames_the_cookie() -> None:
    import httpx
    import pytest

    from poefire.api.session import SessionApi, SessionError

    auth: list[bool] = []

    def site(r: httpx.Request) -> httpx.Response:
        assert r.url.raw_path == b"/api/trade/history/Some%20League"
        assert r.headers["Cookie"] == "POESESSID=x"
        return httpx.Response(403, json={"error": {"code": 6, "message": "Forbidden"}})

    api = SessionApi("x", "Me#1234", "poefire/test", http=httpx.Client(
        transport=httpx.MockTransport(site)), min_interval=0, on_auth=auth.append)  # fmt: skip
    with pytest.raises(SessionError, match="HTTP 403"):
        api.trade_history("Some League")
    assert auth == []  # a refused trade history doesn't mark the cookie as expired


def test_a_rate_limited_trade_site_never_holds_up_loot_tracking() -> None:
    """The trade site hands out long penalties. Waiting one out held the shared connection,
    so loot snapshots stopped (for 10 minutes here, longer for bigger penalties)."""
    import threading

    import httpx
    import pytest

    from poefire.api.session import SessionApi, SessionError

    def site(r: httpx.Request) -> httpx.Response:
        if r.url.path.startswith("/api/trade/history"):
            return httpx.Response(429, json={}, headers={
                "X-Rate-Limit-Policy": "trade-history", "X-Rate-Limit-Rules": "Account",
                "X-Rate-Limit-Account": "3:60:600", "X-Rate-Limit-Account-State": "4:60:600",
                "Retry-After": "600"})  # fmt: skip
        return httpx.Response(200, json=[])

    http = httpx.Client(transport=httpx.MockTransport(site))
    api = SessionApi("x", "Me#1234", "poefire/test", http=http, min_interval=0)
    for _ in range(2):  # the second time, the penalty is already known
        done = threading.Event()

        def trade(done: threading.Event = done) -> None:
            with pytest.raises(SessionError, match="rate limited"):
                api.trade_history("L")
            done.set()

        threading.Thread(target=trade, daemon=True).start()
        assert done.wait(3), "the trade history request waited out the penalty"
        assert api.list_characters() == []  # the loot connection keeps working
