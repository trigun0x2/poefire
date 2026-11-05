"""Path of Building export: item text, gems, tree spec and the code format."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from test_builds import BUILD, WEBSITE_ITEMS, WEBSITE_PASSIVES

from poefire import pob
from poefire.pob_tree import CLASSES, JEWEL_SLOTS


def test_rare_item_text_follows_pobs_format() -> None:
    item: dict[str, Any] = {
        "id": "abc123", "frameType": 2, "name": "Doom Visor", "typeLine": "Hubris Circlet",
        "ilvl": 84, "influences": {"shaper": True, "hunter": True}, "searing": True,
        "properties": [{"name": "Quality", "values": [["+20%", 1]]},
                       {"name": "Energy Shield", "values": [["212", 1]]}],
        "requirements": [{"name": "Level", "values": [["69", 0]]}],
        "sockets": [{"group": 0, "sColour": "B"}, {"group": 0, "sColour": "B"},
                    {"group": 1, "sColour": "R"}],
        "enchantMods": ["Arc has 20% increased Damage"],
        "implicitMods": ["+30 to maximum Life"],
        "explicitMods": ["+90 to maximum Energy Shield", "Adds 1 to 3 Lightning Damage"],
        "fracturedMods": ["+40% to Cold Resistance"],
        "craftedMods": ["+20 to Intelligence"],
        "corrupted": True,
    }  # fmt: skip
    assert pob.item_text(item) == "\n".join([
        "Rarity: RARE", "Doom Visor", "Hubris Circlet", "Energy Shield: 212",
        "Unique ID: abc123", "Shaper Item", "Hunter Item", "Searing Exarch Item",
        "Item Level: 84", "Quality: 20", "Sockets: B-B R", "LevelReq: 69", "Implicits: 2",
        "{crafted}Arc has 20% increased Damage", "+30 to maximum Life",
        "{fractured}+40% to Cold Resistance", "+90 to maximum Energy Shield",
        "Adds 1 to 3 Lightning Damage", "{crafted}+20 to Intelligence", "Corrupted",
    ])  # fmt: skip


def test_item_names_and_special_cases() -> None:
    magic = pob.item_text({"frameType": 1, "typeLine": "Seething Divine Life Flask of Staunching"})
    assert magic is not None and magic.split("\n")[:2] == [
        "Rarity: MAGIC", "Seething Divine Life Flask of Staunching"]  # fmt: skip
    synth = pob.item_text(
        {"frameType": 2, "name": "Grim Bane", "typeLine": "Synthesised Gold Ring"}
    )
    assert synth is not None and synth.split("\n")[2] == "Gold Ring"
    defences = [{"name": "Evasion Rating", "values": [["100", 0]]},
                {"name": "Energy Shield", "values": [["30", 0]]}]  # fmt: skip
    boots = pob.item_text({"frameType": 2, "name": "Tempest Stride",
                           "typeLine": "Two-Toned Boots", "properties": defences})  # fmt: skip
    assert boots is not None and "Two-Toned Boots (Evasion/Energy Shield)" in boots
    oauth_mods = pob.item_text({"frameType": 2, "name": "X", "typeLine": "Gold Ring",
                                "explicitMods": [{"description": "+10 to Strength",
                                                  "flags": {"fractured": True}}]})  # fmt: skip
    assert oauth_mods is not None and "{fractured}+10 to Strength" in oauth_mods
    assert pob.item_text({"frameType": 4, "typeLine": "Arc"}) is None  # gems aren't items
    quote, dash = chr(0x2019), chr(0x2013)  # typographic quote and dash
    assert pob.sanitise(f"Doom{quote}s <<set:MS>>Visor{dash}x") == "Doom?s Visor-x"


def test_gem_names() -> None:
    assert pob.gem_name({"typeLine": "Added Lightning Damage Support"}) == "Added Lightning Damage"
    assert pob.gem_name({"typeLine": "Arc", "hybrid": {"baseTypeName": "Arc of Surging"}}) == (
        "Arc of Surging")  # fmt: skip
    vaal = {"typeLine": "Vaal Arc", "hybrid": {"baseTypeName": "Arc", "isVaalGem": True}}
    assert pob.gem_name(vaal) == "Vaal Arc"


def parse(code: str) -> ET.Element:
    return ET.fromstring(pob.decode(code))


def test_build_xml_from_website_data() -> None:
    root = parse(pob.pob_code(BUILD))
    build = root.find("Build")
    assert build is not None
    assert build.attrib["level"] == "42" and build.attrib["className"] == "Ranger"
    assert build.attrib["ascendClassName"] == "Deadeye" and build.attrib["bandit"] == "Alira"
    spec = root.find("Tree/Spec")
    assert spec is not None
    ranger = next(i for i, (name, _) in enumerate(CLASSES) if name == "Ranger")
    assert spec.attrib["classId"] == str(ranger)
    assert spec.attrib["ascendClassId"] == str(CLASSES[ranger][1].index("Deadeye") + 1)
    assert spec.attrib["nodes"] == "1,2,3,4,70000"  # cluster nodes (hashes_ex) included
    assert spec.attrib["masteryEffects"] == "{5,6}" and spec.attrib["treeVersion"] == "3_29"
    (socket,) = root.findall("Tree/Spec/Sockets/Socket")
    assert socket.attrib["nodeId"] == str(JEWEL_SLOTS[3])  # the jewel's x = 3
    slots = {s.attrib["name"]: s.attrib["itemId"] for s in root.findall("Items/ItemSet/Slot")}
    assert set(slots) == {"Helmet", "Weapon 1", "Ring 2", "Flask 1", "Flask 2"}
    items = {i.attrib["id"]: (i.text or "").strip() for i in root.findall("Items/Item")}
    assert items[slots["Helmet"]].startswith("Rarity: RARE\nDoom Visor\nHubris Circlet")
    assert items[slots["Flask 1"]].endswith("Divine Life Flask\nImplicits: 0")
    assert items[socket.attrib["itemId"]].startswith("Rarity: NORMAL\nCobalt Jewel")
    skills = root.findall("Skills/SkillSet/Skill")
    assert [(s.attrib["slot"], [(g.attrib["nameSpec"], g.attrib["level"], g.attrib["quality"])
                               for g in s]) for s in skills] == [
        ("Helmet", [("Arc", "20", "20"), ("Added Lightning Damage", "18", "20")]),
        ("Helmet", [("Flame Dash", "5", "20")]),
    ]  # fmt: skip
    assert build.attrib["mainSocketGroup"] == "1"  # the biggest link group
    # pobb.in rejects a <Build> without children (PoB always saves some); PoB ignores this one
    assert [c.tag for c in build] == ["TimelessData"]
    inputs = {i.attrib["name"]: i.attrib["string"] for i in root.findall("Config/ConfigSet/Input")}
    assert inputs == {"bandit": "Alira", "pantheonMajorGod": "None", "pantheonMinorGod": "None"}


def test_oauth_ruthless_and_escaping() -> None:
    char = {**WEBSITE_ITEMS["character"], "league": "Ruthless Keepers",
            "equipment": [{"inventoryId": "Amulet", "frameType": 2, "name": "Doom & Gloom",
                           "typeLine": "Gold Amulet"}],
            "jewels": [], "passives": {**WEBSITE_PASSIVES, "bandit_choice": "Eramir"}}  # fmt: skip
    root = parse(pob.pob_code({"source": "oauth", "character": char}))
    assert root.find("Tree/Spec").attrib["treeVersion"] == "3_29_ruthless"  # type: ignore[union-attr]
    assert root.find("Build").attrib["bandit"] == "None"  # type: ignore[union-attr]
    (item,) = root.findall("Items/Item")
    assert "Doom & Gloom" in (item.text or "")  # escaped in the XML, intact once parsed


def test_code_format() -> None:
    code = pob.pob_code(BUILD)
    assert "+" not in code and "/" not in code  # URL-safe alphabet, like PoB's codes
    assert code.startswith("eN")  # zlib stream, which PoB's Inflate expects
    assert pob.decode(code).startswith('<?xml version="1.0"')


def test_realistic_snapshot() -> None:
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).parent / "data" / "build_website.json").read_text())
    root = parse(pob.pob_code(data))
    slots = {s.attrib["name"] for s in root.findall("Items/ItemSet/Slot")}
    assert slots == {"Helmet", "Body Armour", "Ring 1", "Flask 1", "Flask 2", "Weapon 1 Swap"}
    groups = [[g.attrib["nameSpec"] for g in s] for s in root.findall("Skills/SkillSet/Skill")]
    assert groups == [
        ["Tornado Shot", "Greater Multiple Projectiles", "Mirage Archer"],
        ["Flame Dash"],
        ["Lightning Arrow", "Elemental Damage with Attacks"],
    ]
    texts = [(i.text or "").strip() for i in root.findall("Items/Item")]
    helm = next(t for t in texts if "Doom Visor" in t)
    assert "Hunter Item" in helm and "Evasion: 642" in helm and "Sockets: G-G-R B" in helm
    assert "LevelReq" not in helm  # gems can raise requirements, like PoB's importer
    ring = next(t for t in texts if "Rune Coil" in t)
    assert "{fractured}+41 to Dexterity" in ring and ring.endswith("Fractured Item")
    assert "Implicits: 1\n+16% to Fire and Cold Resistances" in ring
    build = root.find("Build")
    assert build is not None and build.attrib["pantheonMajorGod"] == "Lunaris"
