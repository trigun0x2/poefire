"""Which Path of Exile patch (e.g. "3.27") was current at a given time.

The release dates come from the community wiki (poewiki.net), which keeps a table of every
game version; they're fetched at most once a day and kept in the database, so a run started
offline still gets the patch from the last list. Only PoE 1 versions (3.x) count, and a
patch starts on its x.y.0 release.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

WIKI_API = "https://www.poewiki.net/w/api.php"
CACHE_KEY = "poe_patches"
REFRESH_EVERY = timedelta(days=1)
_VERSION = re.compile(r"^\s*(3)\.(\d+)(?:\.|\s|$)")
PATCH = re.compile(r"^\d+\.\d+(\.\d+[a-z]?)?$")  # what a user may type

type Patches = list[tuple[str, str]]  # (release date "YYYY-MM-DD", "3.27"), oldest first


def parse_wiki(data: Any) -> Patches:
    """The first release date of each 3.x patch from a cargoquery response."""
    rows = data.get("cargoquery") if isinstance(data, dict) else None
    first: dict[str, str] = {}
    for row in rows or []:
        fields = row.get("title") if isinstance(row, dict) else None
        if not isinstance(fields, dict):
            continue
        version = str(fields.get("version") or "")
        date = str(fields.get("release date") or fields.get("release_date") or "")[:10]
        m = _VERSION.match(version)
        if not m or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        patch = f"{m.group(1)}.{int(m.group(2))}"
        if patch not in first or date < first[patch]:
            first[patch] = date
    return sorted((d, p) for p, d in first.items())


def patch_at(patches: Iterable[tuple[str, str]], when: datetime) -> str | None:
    """The patch released last on or before `when` (by day)."""
    day = when.strftime("%Y-%m-%d")
    current = None
    for date, patch in sorted(patches):
        if date <= day:
            current = patch
    return current


def fetch(user_agent: str, http: httpx.Client | None = None) -> Patches:
    params = {
        "action": "cargoquery", "format": "json", "tables": "versions",
        "fields": "version,release_date", "where": 'version LIKE "3.%"',
        "order_by": "release_date", "limit": "500",
    }  # fmt: skip
    client = http or httpx.Client(timeout=20)
    try:
        r = client.get(WIKI_API, params=params, headers={"User-Agent": user_agent})
        r.raise_for_status()
        return parse_wiki(r.json())
    finally:
        if http is None:
            client.close()


class PatchBook:
    """The list of patches, from the database, refreshed from the wiki once a day."""

    def __init__(self, load: Callable[[], Any], save: Callable[[Any], None],
                 fetcher: Callable[[], Patches], now: Callable[[], datetime]) -> None:  # fmt: skip
        self._load, self._save, self._fetch, self._now = load, save, fetcher, now
        self.patches: Patches = []
        self.fetched_at: datetime | None = None
        cached = load() or {}
        try:
            self.patches = [(str(d), str(p)) for d, p in cached.get("patches") or []]
            at = cached.get("fetched_at")
            self.fetched_at = datetime.fromisoformat(at) if at else None
        except (TypeError, ValueError, AttributeError):
            self.patches, self.fetched_at = [], None

    def stale(self) -> bool:
        return self.fetched_at is None or self._now() - self.fetched_at >= REFRESH_EVERY

    def refresh(self) -> bool:
        """Fetch the list; True if it worked (the old list stays if it didn't)."""
        try:
            patches = self._fetch()
        except (httpx.HTTPError, ValueError) as e:
            log.info("couldn't fetch the patch list: %s", e)
            return False
        if not patches:
            return False
        self.patches, self.fetched_at = patches, self._now()
        self._save({"patches": patches, "fetched_at": self.fetched_at.isoformat()})
        return True

    def at(self, when: datetime) -> str | None:
        return patch_at(self.patches, when)
