from pathlib import Path

import pytest

from poefire import winfix


def test_unblock_dlls_removes_zone_identifier_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pythonnet" / "runtime").mkdir(parents=True)
    blocked = tmp_path / "pythonnet" / "runtime" / "Python.Runtime.dll"
    clean = tmp_path / "other.dll"
    for f in (blocked, clean, tmp_path / "data.json"):
        f.write_bytes(b"")
    removed: list[str] = []

    def fake_remove(path: str) -> None:
        if path != f"{blocked}:Zone.Identifier":
            raise FileNotFoundError(path)
        removed.append(path)

    monkeypatch.setattr("poefire.winfix.os.remove", fake_remove)
    assert winfix.unblock_dlls(tmp_path) == 1
    assert removed == [f"{blocked}:Zone.Identifier"]
