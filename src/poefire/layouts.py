"""Zone layout guides from Cyclon's Advanced Campaign Guide by CyclonDefinitiv.

All guide content (layout pictures, points of interest, zone reads) is CyclonDefinitiv's:
https://www.definitivguide.com/docs/PoE1/layout, YouTube https://www.youtube.com/@CyclonDefinitiv.
poefire doesn't ship any of it. It links to his page for a zone and shows that page's content,
fetched from his site when asked, with credit, and keeps a copy for a week so his site isn't
asked again for every view.

Pages are found through the site's sitemap (/docs/PoE1/Act N/<Zone>), and matched to the
game's zone names ("The Chamber of Sins Level 1" -> Act 2/Chamber of Sins 1).
"""

from __future__ import annotations

import difflib
import html
import logging
import re
import time
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, unquote, urljoin

import httpx

log = logging.getLogger(__name__)

SITE = "https://www.definitivguide.com"
GUIDE_URL = SITE + "/docs/PoE1/layout"
YOUTUBE_URL = "https://www.youtube.com/@CyclonDefinitiv"
CREDIT = {
    "guide": "Cyclon\u2019s Advanced Campaign Guide",
    "author": "CyclonDefinitiv",
    "site": GUIDE_URL,
    "youtube": YOUTUBE_URL,
}
KEEP_FOR = 7 * 24 * 3600  # seconds
INDEX_KEY = "definitiv_index"
PAGE_KEY = "definitiv_page2:"  # 2: pages now carry the Simple Read

type Json = dict[str, Any]
type Index = dict[str, str]  # "act|normalised zone" -> page url


def normalise(zone: str) -> str:
    z = zone.casefold().replace("\u2019", "'")
    z = re.sub(r"^the\s+", "", z)
    z = z.replace("'", "")
    z = re.sub(r"\blevel\s+(\d)", r"\1", z)
    z = re.sub(r"[^a-z0-9]+", " ", z)
    return z.strip()


def parse_sitemap(xml: str) -> Index:
    index: Index = {}
    for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
        m = re.search(r"/docs/PoE1/Act%20(\d+)/([^/]+)$", loc) or re.search(
            r"/docs/PoE1/Act (\d+)/([^/]+)$", unquote(loc)
        )
        if m is None:
            continue
        zone = unquote(m.group(2))
        if zone.casefold() == "overview":
            continue
        index[f"{int(m.group(1))}|{normalise(zone)}"] = (
            f"{SITE}/docs/PoE1/{quote(f'Act {int(m.group(1))}/{zone}')}"
        )
    return index


def find(index: Index, act: int | None, zone: str) -> str | None:
    """The guide page for a zone of an act, or None."""
    if act is None:
        return None
    name = normalise(zone)
    for candidate in (name, re.sub(r"\s1$", "", name)):
        url = index.get(f"{act}|{candidate}")
        if url:
            return url
    in_act = {k.split("|", 1)[1]: v for k, v in index.items() if k.startswith(f"{act}|")}
    close = difflib.get_close_matches(name, list(in_act), n=1, cutoff=0.88)
    return in_act[close[0]] if close else None


class _Page(HTMLParser):
    """Pulls the layouts, points of interest and zone read out of a guide page."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.in_article = 0
        self.section = ""
        self.heading: list[str] | None = None
        self.images: list[Json] = []
        self.text: dict[str, list[str]] = {}
        self.headers: list[str] = []  # this table's column headers ("Static Layout"...)
        self.table_images = 0
        self._th: list[str] | None = None
        self._td: list[str] | None = None  # a cell's own text labels its pictures
        self._td_first = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "article":
            self.in_article += 1
        if not self.in_article:
            return
        if tag in ("h2", "h3"):
            self.heading = []
        elif tag == "table":
            self.headers, self.table_images = [], 0
        elif tag == "th":
            self._th = []
        elif tag == "td":
            self._td, self._td_first = [], len(self.images)
        elif tag == "img" and a.get("src") and self.section == "layouts":
            i = self.table_images
            header = self.headers[i % len(self.headers)] if self.headers else ""
            label = header or a.get("alt") or ""
            src = quote(urljoin(self.base, a["src"] or ""), safe=":/%?=&")
            self.images.append({"src": src, "label": label})
            self.table_images += 1
        elif tag in ("p", "li", "br") and self.section:
            self.text.setdefault(self.section, []).append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self.in_article -= 1
        elif tag in ("h2", "h3") and self.heading is not None:
            self.section = normalise("".join(self.heading))
            self.heading = None
        elif tag == "th" and self._th is not None:
            self.headers.append(" ".join("".join(self._th).split()))
            self._th = None
        elif tag == "td" and self._td is not None:
            text = " ".join("".join(self._td).split())
            if text:
                for img in self.images[self._td_first :]:
                    img["label"] = text
            self._td = None

    def handle_data(self, data: str) -> None:
        if not self.in_article:
            return
        if self.heading is not None:
            self.heading.append(data)
        elif self._th is not None:
            self._th.append(data)
        elif self._td is not None:
            self._td.append(data)
        elif self.section:
            self.text.setdefault(self.section, []).append(data)

    def lines(self, section: str) -> list[str]:
        raw = "".join(self.text.get(section, []))
        return [" ".join(x.split()) for x in raw.replace("​", "").splitlines() if x.strip()]


_ADMONITION = re.compile(
    r'<div class="theme-admonition[^"]*">.*?<div class="admonitionHeading[^"]*">(.*?)</div>'
    r'\s*<div class="admonitionContent[^"]*">(.*?)</div>\s*</div>',
    re.S,
)


def _text(fragment: str) -> list[str]:
    plain = html.unescape(re.sub(r"<[^>]+>", "\n", fragment)).replace("\u200b", "")
    return [" ".join(x.split()) for x in plain.splitlines() if x.strip()]


def simple_read(page_html: str) -> tuple[list[str], str]:
    """The page's "Simple Read" boxes (the quick version of a zone read), and the page
    without them so they aren't repeated in the zone read."""
    found: list[str] = []

    def take(m: re.Match[str]) -> str:
        heading = " ".join(_text(m.group(1)))
        if normalise(heading) != "simple read":
            return m.group(0)
        found.extend(_text(m.group(2)))
        return ""

    return found, _ADMONITION.sub(take, page_html)


def parse_page(page_html: str, url: str) -> Json:
    simple, page_html = simple_read(page_html)
    p = _Page(url)
    p.feed(page_html)
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.S)
    return {
        "url": url,
        "title": html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else "",
        "layouts": p.images[:12],
        "points_of_interest": p.lines("points of interest"),
        "simple_read": simple,
        "zone_read": p.lines("zone read"),
    }


class LayoutGuide:
    """Finds and reads guide pages, cached in the database for a week."""

    def __init__(self, load: Callable[[str], Any], save: Callable[[str, Any], None],
                 user_agent: str, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        self._load, self._save, self.clock = load, save, clock
        self.http = http or httpx.Client(timeout=15, follow_redirects=True,
                                         headers={"User-Agent": user_agent})  # fmt: skip

    def _cached(self, key: str) -> Any:
        hit = self._load(key)
        if isinstance(hit, dict) and self.clock() - float(hit.get("at", 0)) < KEEP_FOR:
            return hit.get("data")
        return None

    def _get(self, url: str) -> str:
        r = self.http.get(url)
        r.raise_for_status()
        return r.text

    def index(self) -> Index:
        cached = self._cached(INDEX_KEY)
        if cached:
            return dict(cached)
        try:
            index = parse_sitemap(self._get(SITE + "/sitemap.xml"))
        except httpx.HTTPError as e:
            log.info("layout guide index unavailable: %s", e)
            stale = self._load(INDEX_KEY)
            return dict(stale.get("data") or {}) if isinstance(stale, dict) else {}
        if index:
            self._save(INDEX_KEY, {"at": self.clock(), "data": index})
        return index

    def zone(self, act: int | None, zone: str) -> Json | None:
        """The guide for a zone: its page url, layout pictures, points of interest and zone
        read; just the url if the page couldn't be read. None when there's no page for it."""
        url = find(self.index(), act, zone)
        if url is None:
            return None
        cached = self._cached(PAGE_KEY + url)
        if cached:
            return dict(cached)
        try:
            page = parse_page(self._get(url), url)
        except httpx.HTTPError as e:
            log.info("layout guide page %s unavailable: %s", url, e)
            return {"url": url, "title": zone, "layouts": [], "points_of_interest": [],
                    "simple_read": [], "zone_read": []}  # fmt: skip
        self._save(PAGE_KEY + url, {"at": self.clock(), "data": page})
        return page
