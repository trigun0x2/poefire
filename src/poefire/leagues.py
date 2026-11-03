"""League names: private leagues, and which league to price items from."""

from __future__ import annotations

import re
from collections.abc import Iterable

# Private leagues are named "<chosen name> (PL12345)".
_PRIVATE_RE = re.compile(r"\(PL\d+\)\s*$")
_NOT_A_TRADE_PARENT = ("Standard", "Hardcore", "HC", "SSF", "Solo Self-Found", "Ruthless")


def is_private(league: str) -> bool:
    return bool(_PRIVATE_RE.search(league))


def default_price_league(league: str, ninja_leagues: Iterable[str]) -> str:
    """poe.ninja has no data for private leagues. Price them from the current softcore
    challenge league (what most private leagues are based on); the user can override this in
    Settings, e.g. for a hardcore private league. Other leagues price from themselves."""
    if not is_private(league):
        return league
    for name in ninja_leagues:
        if not any(word in name for word in _NOT_A_TRADE_PARENT):
            return name
    return "Standard"
