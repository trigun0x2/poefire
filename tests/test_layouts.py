"""The worst zones of a campaign run, and finding/reading CyclonDefinitiv's layout guide pages
(with made-up page content: none of his guide is copied into the tests)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from poefire import layouts
from poefire.campaign import CampaignRun, Split, SplitKind, best_splits, worst_zones, zone_times

T0 = datetime(2026, 11, 6, 12)
SITEMAP = """<urlset>
<url><loc>https://www.definitivguide.com/docs/PoE1/Act%202/Crossroads</loc></url>
<url><loc>https://www.definitivguide.com/docs/PoE1/Act%202/Chamber%20of%20Sins%201</loc></url>
<url><loc>https://www.definitivguide.com/docs/PoE1/Act%202/overview</loc></url>
<url><loc>https://www.definitivguide.com/docs/PoE1/Act%207/Chamber%20of%20Sins</loc></url>
<url><loc>https://www.definitivguide.com/docs/PoE1/Act%208/Doedre%27s%20Cespool</loc></url>
<url><loc>https://www.definitivguide.com/docs/PoE1/layout</loc></url>
</urlset>"""
PAGE = """<html><body><nav>menu</nav><article><header><h1>Test Zone</h1></header>
<h2 id="layouts">Layouts<a href="#layouts">&#8203;</a></h2>
<table><thead><tr><th>Variant A</th><th></th></tr></thead><tbody><tr>
<td><img alt="Test Zone" src="/assets/images/Zone (1)-abc.png"></td>
<td>Variant B text <img alt="Test Zone" src="/assets/images/zone2.png"></td></tr></tbody></table>
<h3 id="points-of-interest">Points of Interest</h3><p>Waypoint
Some Chest</p>
<h2 id="zone-read">Zone Read</h2>
<div class="theme-admonition theme-admonition-tip alert"><div class="admonitionHeading_x"><span>
<svg><path d="M0"></path></svg></span>Simple Read</div><div class="admonitionContent_y">
<p>Exit is always east.</p></div></div>
<div class="theme-admonition theme-admonition-note alert">
<div class="admonitionHeading_x">Note</div>
<div class="admonitionContent_y"><p>Other box.</p></div></div><p>Go north first.
Then east.</p></article><footer>Copyright</footer></body></html>"""


def test_find_matches_game_zone_names() -> None:
    index = layouts.parse_sitemap(SITEMAP)
    assert len(index) == 4  # overview and other docs are skipped
    base = "https://www.definitivguide.com/docs/PoE1/"
    assert layouts.find(index, 2, "The Crossroads") == base + "Act%202/Crossroads"
    assert (
        layouts.find(index, 2, "The Chamber of Sins Level 1")
        == base + "Act%202/Chamber%20of%20Sins%201"
    )
    assert (
        layouts.find(index, 7, "The Chamber of Sins Level 1")
        == base + "Act%207/Chamber%20of%20Sins"
    )
    assert layouts.find(index, 8, "Doedre's Cesspool") == base + "Act%208/Doedre%27s%20Cespool"
    assert layouts.find(index, 3, "The Crossroads") is None  # another act
    assert layouts.find(index, None, "The Crossroads") is None


def test_parse_page() -> None:
    g = layouts.parse_page(PAGE, "https://www.definitivguide.com/docs/PoE1/Act%201/Test")
    assert g["title"] == "Test Zone"
    images = "https://www.definitivguide.com/assets/images/"
    assert g["layouts"] == [
        {"src": images + "Zone%20%281%29-abc.png", "label": "Variant A"},
        {"src": images + "zone2.png", "label": "Variant B text"},
    ]
    assert g["points_of_interest"] == ["Waypoint", "Some Chest"]
    assert g["simple_read"] == ["Exit is always east."]
    assert g["zone_read"] == ["Note", "Other box.", "Go north first.", "Then east."]


def test_guide_is_fetched_once_and_kept() -> None:
    hits: list[str] = []

    def site(r: httpx.Request) -> httpx.Response:
        hits.append(r.url.raw_path.decode())
        if r.url.path == "/sitemap.xml":
            return httpx.Response(200, text=SITEMAP)
        return httpx.Response(200, text=PAGE)

    kv: dict[str, Any] = {}
    now = [1000.0]
    guide = layouts.LayoutGuide(lambda k: kv.get(k), kv.__setitem__, "poefire/test",
                                httpx.Client(transport=httpx.MockTransport(site)),
                                clock=lambda: now[0])  # fmt: skip
    g = guide.zone(2, "The Crossroads")
    assert g is not None and g["simple_read"] == ["Exit is always east."]
    assert guide.zone(2, "The Crossroads") == g
    assert hits == ["/sitemap.xml", "/docs/PoE1/Act%202/Crossroads"]  # second time from the db
    assert guide.zone(5, "Nowhere") is None
    now[0] += layouts.KEEP_FOR + 1  # a week later: read again
    guide.zone(2, "The Crossroads")
    assert len(hits) == 4

    def down(r: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    offline = layouts.LayoutGuide(lambda k: None, lambda k, v: None, "poefire/test",
                                  httpx.Client(transport=httpx.MockTransport(down)))  # fmt: skip
    assert offline.zone(2, "The Crossroads") is None  # no index: nothing to link to


def run(rid: int, zone_secs: list[float], status: str = "finished", act: int = 3) -> CampaignRun:
    r = CampaignRun(started_at=T0, id=rid, status=status, char_class="Witch", act=act)
    t = 0.0
    for i, sec in enumerate(zone_secs):
        r.splits.append(Split(SplitKind.ZONE, f"Zone {i}", 1, T0 + timedelta(seconds=t), t, t))
        t += sec
    if status != "running":
        r.ended_at = T0 + timedelta(seconds=t)
    return r


def test_zone_times() -> None:
    assert [round(t) for _, t in zone_times(run(1, [60, 120, 30]))] == [60, 120, 30]
    assert [round(t) for _, t in zone_times(run(1, [60, 120, 30], "running"))] == [60, 120]
    assert [round(t) for _, t in zone_times(run(1, [60, 120, 30], "abandoned"))] == [60, 120]


def test_worst_zones() -> None:
    a, b = run(1, [60, 100, 40]), run(2, [50, 140, 30])
    bests = best_splits([a, b], "Witch")
    assert bests.zone_segments == {(1, "Zone 0"): 50, (1, "Zone 1"): 100, (1, "Zone 2"): 30}
    mine = run(3, [55, 160, 70])
    worst = worst_zones(mine, a, bests)
    assert [(w["zone"], w["lost"], w["basis"]) for w in worst] == [
        ("Zone 1", 60, "best"), ("Zone 2", 40, "best"), ("Zone 0", 5, "best")]  # fmt: skip
    by_pb = worst_zones(mine, a, None)[0]
    assert by_pb == {"act": 1, "zone": "Zone 1", "seconds": 160, "reference": 100,
                     "basis": "pb", "lost": 60}  # fmt: skip
    assert worst_zones(run(4, [40, 90, 20]), a, bests) == []  # faster everywhere
    first = worst_zones(mine, None, None)  # nothing to compare with: the longest zones
    assert [w["zone"] for w in first] == ["Zone 1", "Zone 2", "Zone 0"]
    assert first[0]["basis"] == "longest"
