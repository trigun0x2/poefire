import pytest

from poefire.zones import ZoneKind, ZoneRules


@pytest.mark.parametrize(
    ("area_id", "kind"),
    [
        ("HideoutFelled", ZoneKind.HIDEOUT),
        ("HideoutCelestialNebula", ZoneKind.HIDEOUT),
        ("1_1_town", ZoneKind.TOWN),
        ("2_10_town", ZoneKind.TOWN),
        ("HeistHub", ZoneKind.TOWN),
        ("MapWorldsStrand", ZoneKind.MAP),
        ("1_1_2", ZoneKind.OTHER),
        ("Labyrinth_Airlock", ZoneKind.OTHER),
    ],
)
def test_classify(area_id: str, kind: ZoneKind) -> None:
    assert ZoneRules().classify(area_id) is kind


def test_extras_and_exclusions() -> None:
    rules = ZoneRules.with_extras(
        extra_map_prefixes=["Heist"], extra_town_ids=["MyTown"], excluded_ids=["MapFake"]
    )
    assert rules.classify("HeistDungeon1") is ZoneKind.MAP
    assert rules.classify("HeistHub") is ZoneKind.TOWN  # town wins over prefix
    assert rules.classify("MyTown") is ZoneKind.TOWN
    assert rules.classify("MapFake") is ZoneKind.OTHER


def test_classify_name_fallback() -> None:
    rules = ZoneRules()
    assert rules.classify_name("Felled Hideout") is ZoneKind.HIDEOUT
    assert rules.classify_name("Lioneye's Watch") is ZoneKind.TOWN
    assert rules.classify_name("Strand") is ZoneKind.OTHER
