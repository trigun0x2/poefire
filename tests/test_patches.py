from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from poefire import patches

WIKI: dict[str, Any] = {
    "cargoquery": [
        {"title": {"version": "3.26.0", "release date": "2025-06-13"}},
        {"title": {"version": "3.26.0b", "release date": "2025-06-16"}},
        {"title": {"version": "3.25.3d", "release date": "2025-02-20"}},
        {"title": {"version": "3.25.0", "release date": "2024-07-26 20:00:00"}},
        {"title": {"version": "3.27.0", "release date": "2025-10-31"}},
        {"title": {"version": "0.3.0", "release date": "2025-08-29"}},  # PoE 2: not ours
        {"title": {"version": "3.28.0"}},  # no date yet
        "junk",
    ]
}


def test_parse_and_patch_at() -> None:
    ps = patches.parse_wiki(WIKI)
    assert ps == [("2024-07-26", "3.25"), ("2025-06-13", "3.26"), ("2025-10-31", "3.27")]
    assert patches.patch_at(ps, datetime(2025, 6, 12, 23)) == "3.25"
    assert patches.patch_at(ps, datetime(2025, 6, 13, 1)) == "3.26"
    assert patches.patch_at(ps, datetime(2026, 1, 1)) == "3.27"
    assert patches.patch_at(ps, datetime(2020, 1, 1)) is None
    assert patches.parse_wiki({"error": "x"}) == [] and patches.parse_wiki(None) == []
    assert patches.PATCH.match("3.27") and patches.PATCH.match("3.27.1b")
    assert not patches.PATCH.match("soon")


def test_fetch_and_book() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=WIKI)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert patches.fetch("poefire/test", http)[-1] == ("2025-10-31", "3.27")
    assert seen[0].url.host == "www.poewiki.net" and seen[0].headers["User-Agent"] == "poefire/test"

    saved: dict[str, Any] = {}
    now = [datetime(2026, 1, 1)]
    book = patches.PatchBook(lambda: None, saved.update, lambda: patches.parse_wiki(WIKI),
                             lambda: now[0])  # fmt: skip
    assert book.stale() and book.at(now[0]) is None
    assert book.refresh() and book.at(now[0]) == "3.27" and not book.stale()
    again = patches.PatchBook(lambda: saved, saved.update, lambda: [], lambda: now[0])
    assert again.at(now[0]) == "3.27" and not again.stale()  # from the database
    assert not again.refresh() and again.at(now[0]) == "3.27"  # an empty answer keeps the list

    def offline() -> patches.Patches:
        raise httpx.ConnectError("no network")

    broken = patches.PatchBook(lambda: {"patches": "bad"}, saved.update, offline, lambda: now[0])
    assert not broken.refresh() and broken.at(now[0]) is None
