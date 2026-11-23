"""League names: private leagues, and which league to price items from."""

from __future__ import annotations

import re
from collections.abc import Iterable

# Private leagues are named "<chosen name> (PL12345)".
_PRIVATE_RE = re.compile(r"\(PL\d+\)\s*$")
_NOT_A_TRADE_PARENT = ("Standard", "Hardcore", "HC", "SSF", "Solo Self-Found", "Ruthless")


def is_private(league: str) -> bool:
    return bool(_PRIVATE_RE.search(league))


# Solo Self-Found leagues: "SSF Covens", "HC SSF Covens", "SSF Standard", "SSF Hardcore",
# "SSF Ruthless Covens"... (also spelled out, "Solo Self-Found").
_SSF_RE = re.compile(r"\b(?:SSF|Solo Self-Found)\b\s*", re.IGNORECASE)
_HC_RE = re.compile(r"^(?:HC|Hardcore)\b\s*", re.IGNORECASE)


def is_ssf(league: str) -> bool:
    return bool(_SSF_RE.search(league))


def trade_league(league: str) -> str:
    """The trade league an SSF league's items are priced from (poe.ninja has no SSF data):
    "SSF Covens" -> "Covens", "HC SSF Covens" -> "Hardcore Covens", "SSF Hardcore" ->
    "Hardcore". Other leagues are their own."""
    if not is_ssf(league):
        return league
    rest = " ".join(_SSF_RE.sub(" ", league).split())
    hardcore = bool(_HC_RE.match(rest))
    rest = _HC_RE.sub("", rest).strip()
    if not rest or rest.lower() in ("standard", "hardcore"):
        return "Hardcore" if hardcore or rest.lower() == "hardcore" else "Standard"
    return f"Hardcore {rest}" if hardcore else rest


def latest_league(ninja_leagues: Iterable[str]) -> str:
    """The current softcore challenge league (poe.ninja lists it first), else Standard."""
    for name in ninja_leagues:
        if name and not is_private(name) and not any(w in name for w in _NOT_A_TRADE_PARENT):
            return name
    return "Standard"


def default_price_league(league: str, ninja_leagues: Iterable[str]) -> str:
    """poe.ninja has no data for private or SSF leagues. Price a private league from the
    current softcore challenge league (what most private leagues are based on; the user can
    override this in Settings, e.g. for a hardcore private league), an SSF league from its
    trade league. Other leagues price from themselves."""
    if is_ssf(league) and not is_private(league):
        return trade_league(league)
    if not is_private(league):
        return league
    return latest_league(ninja_leagues)
