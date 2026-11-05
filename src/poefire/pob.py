"""Path of Building codes from build snapshots.

A PoB code is the build as PoB's XML, zlib-compressed and base64-encoded (URL-safe alphabet).
The XML is what PoB itself would save after "Import from website", so this follows PoB's
importer (src/Classes/ImportTab.lua: ImportItem, ImportSocketedItems,
ImportPassiveTreeAndJewels) and its item text format (src/Classes/Item.lua: BuildRaw).
PoB re-parses everything on import (item bases and mods, gems, the tree), so only the
inputs it reads are written here.
"""

from __future__ import annotations

import base64
import re
import zlib
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx

from poefire.builds import parts
from poefire.pob_tree import CLASSES, JEWEL_SLOTS, TREE_VERSION

type Json = dict[str, Any]

RARITY = {0: "NORMAL", 1: "MAGIC", 2: "RARE", 3: "UNIQUE", 9: "RELIC", 10: "RELIC"}
SLOT = {
    "Weapon": "Weapon 1", "Offhand": "Weapon 2", "Weapon2": "Weapon 1 Swap",
    "Offhand2": "Weapon 2 Swap", "Helm": "Helmet", "BodyArmour": "Body Armour",
    "Gloves": "Gloves", "Boots": "Boots", "Amulet": "Amulet", "Ring": "Ring 1",
    "Ring2": "Ring 2", "Ring3": "Ring 3", "Belt": "Belt", "BrequelGrafts": "Graft 1",
    "BrequelGrafts2": "Graft 2",
}  # fmt: skip
INFLUENCES = (("shaper", "Shaper"), ("elder", "Elder"), ("warlord", "Warlord"),
              ("hunter", "Hunter"), ("crusader", "Crusader"), ("redeemer", "Redeemer"))  # fmt: skip
CATALYSTS = ("Attack", "Speed", "Suffix", "Life and Mana", "Caster", "Attribute",
             "Physical and Chaos Damage", "Resistance", "Prefix", "Defense",
             "Elemental Damage", "Critical")  # fmt: skip
TWO_TONED = "Two-Toned Boots"

_DASHES = {cp: "-" for cp in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)}


def sanitise(text: str | None) -> str:
    """PoB's sanitiseText: drop <markup>, ASCII dashes, a/o for umlauts, ? for the rest."""
    if not text:
        return ""
    if not re.search(r"[^\x00-\x7f]|<", text):  # PoB only cleans text that needs it
        return text
    while True:  # like Lua's %b<>: drop balanced <...> groups, nested ones included
        stripped = re.sub(r"<[^<>]*>", "", text)
        if stripped == text:
            break
        text = stripped
    text = text.translate(_DASHES)
    text = text.replace("ä", "a").replace("ö", "o")
    return re.sub(r"[^\x00-\x7f]", "?", text)


def _num(s: Any) -> int | None:
    m = re.search(r"\d+", str(s))
    return int(m.group()) if m else None


def _lines(mods: Any) -> list[tuple[str, Json]]:
    """Mod lines with their flags. Website mods are strings; OAuth ones may be
    {description, flags} objects. Multi-line mods are split like PoB does."""
    out: list[tuple[str, Json]] = []
    for mod in mods or ():
        if isinstance(mod, dict):
            text, flags = str(mod.get("description", "")), mod.get("flags") or {}
        else:
            text, flags = str(mod), {}
        out.extend((line, flags) for line in text.split("\n") if line)
    return out


def _mod(
    line: str,
    *,
    crafted: bool = False,
    fractured: bool = False,
    mutated: bool = False,
    scourge: bool = False,
    crucible: bool = False,
    vestigial: bool = False,
) -> str:
    """A mod line with PoB's tags, in the order Item:BuildRaw writes them (the last tag it
    prepends comes first)."""
    for tag, on in (
        ("crafted", crafted),
        ("scourge", scourge),
        ("crucible", crucible),
        ("mutated", mutated),
        ("fractured", fractured),
        ("vestigial", vestigial),
    ):
        if on:
            line = "{" + tag + "}" + line
    return line


def item_text(item: Json) -> str | None:
    """PoB's text form of a website/OAuth item, or None for items PoB doesn't import."""
    rarity = RARITY.get(int(item.get("frameType", 0) or 0))
    if rarity is None:
        return None
    props = {p.get("name"): p for p in item.get("properties") or ()}
    out = [f"Rarity: {rarity}"]
    if item.get("name"):
        base = re.sub(
            r"^Vestigial ", "", sanitise(item.get("typeLine")).replace("Synthesised ", "")
        )
        if base == TWO_TONED:
            base = _two_toned(props)
        out += [sanitise(item["name"]), base]
    else:
        name = sanitise(item.get("typeLine"))
        if TWO_TONED in name and "(" not in name:
            name = name.replace(TWO_TONED, _two_toned(props))
        out.append(name)
    for key, label in (("Armour", "Armour"), ("Evasion Rating", "Evasion"),
                       ("Energy Shield", "Energy Shield"), ("Ward", "Ward")):  # fmt: skip
        values = props.get(key, {}).get("values") or []
        total = sum(_num(v[0]) or 0 for v in values)
        if total > 0:
            out.append(f"{label}: {total}")
    if item.get("id"):
        out.append(f"Unique ID: {item['id']}")
    influences = item.get("influences") or {}
    out += [f"{label} Item" for key, label in INFLUENCES if influences.get(key)]
    if item.get("searing"):
        out.append("Searing Exarch Item")
    if item.get("tangled"):
        out.append("Eater of Worlds Item")
    for name, prop in props.items():
        m = re.fullmatch(r"Quality \((.*) Modifiers\)", str(name))
        if m and m.group(1) in CATALYSTS:
            out.append(f"Catalyst: {m.group(1)}")
            out.append(f"CatalystQuality: {_num(prop['values'][0][0])}")
    if int(item.get("ilvl") or 0) > 0:
        out.append(f"Item Level: {item['ilvl']}")
    if "Memory Strands" in props:
        out.append(f"Memory Strands: {props['Memory Strands']['values'][0][0]}")
    if "Quality" in props:
        out.append(f"Quality: {_num(props['Quality']['values'][0][0]) or 0}")
    sockets = item.get("sockets") or []
    if sockets:
        line = ""
        for i, s in enumerate(sockets):
            line += str(s.get("sColour", "W"))
            if i + 1 < len(sockets):
                line += "-" if s.get("group") == sockets[i + 1].get("group") else " "
        out.append("Sockets: " + line)
    if not item.get("socketedItems"):  # gems can raise an item's requirements
        for req in item.get("requirements") or ():
            if req.get("name") == "Level":
                out.append(f"LevelReq: {req['values'][0][0]}")
    if "Radius" in props:
        out.append(f"Radius: {props['Radius']['values'][0][0]}")
    if "Limited to" in props:
        out.append(f"Limited to: {props['Limited to']['values'][0][0]}")
    for req in item.get("requirements") or ():
        if req.get("name") == "Class:":
            out.append(f"Requires Class {req['values'][0][0]}")
    enchants = [_mod(line, crafted=True) for line, _ in _lines(item.get("enchantMods"))]
    scourge = [_mod(line, scourge=True) for line, _ in _lines(item.get("scourgeMods"))]
    implicits = [_mod(line, vestigial=bool(f.get("vestigial")))
                 for line, f in _lines(item.get("implicitMods"))]  # fmt: skip
    out.append(f"Implicits: {len(enchants) + len(scourge) + len(implicits)}")
    out += enchants + scourge + implicits
    out += [_mod(line, fractured=True) for line, _ in _lines(item.get("fracturedMods"))]
    out += [
        _mod(line, fractured=bool(f.get("fractured")), crafted=bool(f.get("crafted")),
             mutated=bool(f.get("mutated")), vestigial=bool(f.get("vestigial")))
        for line, f in _lines(item.get("explicitMods"))
    ]  # fmt: skip
    out += [_mod(line, crucible=True) for line, _ in _lines(item.get("crucibleMods"))]
    out += [_mod(line, crafted=True) for line, _ in _lines(item.get("craftedMods"))]
    out += [_mod(line, mutated=True) for line, _ in _lines(item.get("mutatedMods"))]
    for line in item.get("flavourText") or ():  # e.g. "{This item can be anointed by Cassia}"
        m = re.search(r"\{(.*?)\}", line)
        if m and "anoint" in m.group(1):
            out.append(m.group(1))
    if item.get("split"):
        out.append("Split")
    if item.get("mirrored"):
        out.append("Mirrored")
    if item.get("fractured"):
        out.append("Fractured Item")
    if item.get("corrupted"):
        out.append("Corrupted")
    return "\n".join(out)


def _two_toned(props: Json) -> str:
    """PoB's hack: Two-Toned Boots exist in three bases, told apart by their defences."""
    if "Evasion Rating" in props and "Energy Shield" in props:
        return "Two-Toned Boots (Evasion/Energy Shield)"
    if "Evasion Rating" in props:
        return "Two-Toned Boots (Armour/Evasion)"
    return "Two-Toned Boots (Armour/Energy Shield)"


def gem_name(gem: Json) -> str:
    """PoB's gem name: the item name without " Support" (transfigured and dual-skill gems
    carry theirs in `hybrid.baseTypeName`)."""
    hybrid = gem.get("hybrid") or {}
    name = sanitise(gem.get("typeLine"))
    if hybrid.get("baseTypeName") and not hybrid.get("isVaalGem"):
        name = sanitise(hybrid["baseTypeName"])
    return name.removesuffix(" Support")


def _class_ids(char_class: str) -> tuple[int, int]:
    """(classId, ascendClassId) for a class or ascendancy name."""
    for class_id, (name, ascendancies) in enumerate(CLASSES):
        if char_class == name:
            return class_id, 0
        if char_class in ascendancies:
            return class_id, ascendancies.index(char_class) + 1
    return 0, 0


def build_xml(data: Json, level: int | None = None) -> str:
    """PoB's build XML for a snapshot's raw data (website or OAuth)."""
    char, equipped, passives, jewels = parts(data)
    items: list[tuple[int, str]] = []
    slots: list[tuple[str, int]] = []
    sockets: list[tuple[int, int]] = []
    groups: list[tuple[str, list[Json]]] = []

    def add(item: Json) -> int | None:
        text = item_text(item)
        if text is None:
            return None
        items.append((len(items) + 1, text))
        return len(items)

    for item in equipped:
        inv = item.get("inventoryId")
        slot = f"Flask {int(item.get('x', 0)) + 1}" if inv == "Flask" else SLOT.get(inv or "")
        if slot is None:
            continue
        item_id = add(item)
        if item_id is not None:
            slots.append((slot, item_id))
        abyss = 0
        by_group: dict[int, list[Json]] = {}
        item_sockets = item.get("sockets") or []
        for socketed in item.get("socketedItems") or ():
            if socketed.get("abyssJewel"):
                abyss += 1
                if (jewel_id := add(socketed)) is not None:
                    slots.append((f"{slot} Abyssal Socket {abyss}", jewel_id))
                continue
            gem: Json = {"nameSpec": gem_name(socketed), "level": 20, "quality": 0}
            for p in socketed.get("properties") or ():
                if p.get("name") == "Level":
                    gem["level"] = _num(p["values"][0][0]) or 20
                elif p.get("name") == "Quality":
                    gem["quality"] = _num(p["values"][0][0]) or 0
            idx = socketed.get("socket")
            group = item_sockets[idx].get("group", 0) if isinstance(idx, int) and idx < len(
                item_sockets) else 0  # fmt: skip
            by_group.setdefault(int(group), []).append(gem)
        groups += [(slot, by_group[g]) for g in sorted(by_group)]
    for jewel in jewels:
        x = jewel.get("x")
        if isinstance(x, int) and 0 <= x < len(JEWEL_SLOTS) and (jid := add(jewel)) is not None:
            sockets.append((JEWEL_SLOTS[x], jid))

    char_class = str(char.get("class") or "")
    class_id, ascend_id = _class_ids(char_class)
    alternate = passives.get("alternate_ascendancy")
    secondary = alternate if isinstance(alternate, int) else 0
    league = str(char.get("league") or "")
    tree_version = TREE_VERSION + ("_ruthless" if "Ruthless" in league else "")
    nodes = [*(passives.get("hashes") or []), *(passives.get("hashes_ex") or [])]
    masteries = ",".join(
        f"{{{k},{v}}}" for k, v in (passives.get("mastery_effects") or {}).items()
        if int(v) < 65536  # PoB ignores GGG's own codes from profile imports
    )  # fmt: skip
    bandit = passives.get("bandit_choice")
    bandit = "None" if not bandit or bandit == "Eramir" else str(bandit)
    major = str(passives.get("pantheon_major") or "None")
    minor = str(passives.get("pantheon_minor") or "None")
    main_group = max(range(len(groups)), key=lambda i: len(groups[i][1]), default=0) + 1
    level = int(char.get("level") or level or 1)

    def tag(elem: str, close: bool = False, /, **kv: Any) -> str:
        attrs = "".join(f" {k}={quoteattr(str(v))}" for k, v in kv.items())
        return f"<{elem}{attrs}{'/' if close else ''}>"

    build = tag(
        "Build", True, level=level, targetVersion="3_0", className=CLASSES[class_id][0],
        ascendClassName=char_class if ascend_id else "None", mainSocketGroup=main_group,
        viewMode="TREE", bandit=bandit, pantheonMajorGod=major, pantheonMinorGod=minor,
    )  # fmt: skip
    spec = tag(
        "Spec", title=f"{char.get('name', '')} level {level}", treeVersion=tree_version,
        clusterHashFormatVersion=2, classId=class_id, ascendClassId=ascend_id,
        secondaryAscendClassId=secondary, nodes=",".join(map(str, nodes)),
        masteryEffects=masteries,
    )  # fmt: skip
    x = ['<?xml version="1.0" encoding="UTF-8"?>', "<PathOfBuilding>", build]
    x += [tag("Tree", activeSpec=1), spec, "<Sockets>"]
    x += [tag("Socket", True, nodeId=n, itemId=i) for n, i in sockets]
    x += ["</Sockets>", "</Spec>", "</Tree>", tag("Items", activeItemSet=1)]
    x += [f"{tag('Item', id=i)}\n{escape(text)}\n</Item>" for i, text in items]
    x.append(tag("ItemSet", id=1, useSecondWeaponSet="false"))
    x += [tag("Slot", True, name=s, itemId=i) for s, i in slots]
    x += ["</ItemSet>", "</Items>"]
    x.append(tag("Skills", activeSkillSet=1, defaultGemLevel="normalMaximum", defaultGemQuality=0))
    x.append(tag("SkillSet", id=1))
    for slot, gems in groups:
        x.append(tag("Skill", enabled="true", slot=slot, mainActiveSkill=1))
        x += [
            tag("Gem", True, nameSpec=g["nameSpec"], level=g["level"], quality=g["quality"],
                enabled="true")
            for g in gems
        ]  # fmt: skip
        x.append("</Skill>")
    x += ["</SkillSet>", "</Skills>"]
    x += [tag("Config", activeConfigSet=1), tag("ConfigSet", id=1, title="Default")]
    x += [
        tag("Input", True, name=k, string=v)
        for k, v in (("bandit", bandit), ("pantheonMajorGod", major), ("pantheonMinorGod", minor))
    ]
    x += ["</ConfigSet>", "</Config>", "</PathOfBuilding>"]
    return "\n".join(x)


def encode(xml: str) -> str:
    """PoB's code format: zlib-compressed XML in URL-safe base64."""
    return base64.urlsafe_b64encode(zlib.compress(xml.encode("utf-8"), 9)).decode("ascii")


def decode(code: str) -> str:
    """The XML in a PoB code (for tests and checks)."""
    return zlib.decompress(base64.urlsafe_b64decode(code.strip())).decode("utf-8")


def pob_code(data: Json, level: int | None = None) -> str:
    return encode(build_xml(data, level))


# --- opening in Path of Building ---

POBBIN_UPLOAD = "https://pobb.in/pob/"  # what PoB's own "Share" posts to (BuildSiteTools.lua)
_POBBIN_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class PobError(Exception):
    pass


def upload_pobbin(code: str, http: httpx.Client, user_agent: str) -> str:
    """Upload a code to pobb.in (an unlisted paste) and return its id."""
    try:
        r = http.post(POBBIN_UPLOAD, content=code.encode("ascii"),
                      headers={"User-Agent": user_agent,
                               "Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    except httpx.HTTPError as e:
        raise PobError(f"couldn't reach pobb.in: {e}") from e
    if r.status_code != 200:
        raise PobError(f"pobb.in answered HTTP {r.status_code}")
    paste_id = r.text.strip().rsplit("/", 1)[-1]
    if not _POBBIN_ID.match(paste_id):
        raise PobError("pobb.in sent back something that isn't a paste id")
    return paste_id


def pobbin_url(paste_id: str) -> str:
    return f"https://pobb.in/{paste_id}"


def pob_uri(paste_id: str) -> str:
    """Opens Path of Building straight into the build (its pob:// protocol handler)."""
    return f"pob://pobbin/{paste_id}"


def open_uri(uri: str) -> bool:
    """Hand a pob:// link to Windows. False when nothing is registered for it."""
    import os
    import sys

    if sys.platform != "win32":
        return False
    try:
        os.startfile(uri)
    except OSError:
        return False
    return True
