import os
from pathlib import Path

from poefire.tailer import LogTailer, find_last_line_offset, head_fingerprint


def append(path: Path, data: str) -> None:
    with open(path, "ab") as f:
        f.write(data.encode())


def texts(t: LogTailer) -> list[str]:
    return [line.text for line in t.poll()]


def test_starts_at_end_by_default(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text("old 1\nold 2\n")
    t = LogTailer(log)
    assert texts(t) == []
    append(log, "new\n")
    assert texts(t) == ["new"]


def test_partial_lines_wait_for_newline(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text("")
    t = LogTailer(log)
    append(log, "hel")
    assert texts(t) == []
    assert t.offset == 0
    append(log, "lo\r\nwor")
    assert texts(t) == ["hello"]
    append(log, "ld\n")
    assert texts(t) == ["world"]
    assert t.offset == log.stat().st_size


def test_end_offsets_are_resumable(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_bytes(b"a\nbb\nccc\n")
    lines = LogTailer(log, start_offset=0).poll()
    assert [line.end_offset for line in lines] == [2, 5, 9]
    assert texts(LogTailer(log, start_offset=lines[0].end_offset)) == ["bb", "ccc"]


def test_truncation_restarts_from_zero(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text("x" * 100 + "\n")
    t = LogTailer(log)
    log.write_text("fresh\n")  # same inode, smaller
    assert texts(t) == ["fresh"]
    assert t.resets == 1


def test_replaced_file_restarts_from_zero(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text("old content that is long enough\n")
    t = LogTailer(log)
    replacement = tmp_path / "new.txt"
    replacement.write_text("brand new file, longer than the previous one!!\n")
    keep = tmp_path / "keep.txt"
    os.replace(log, keep)  # keep the old inode alive so the new file gets a different one
    os.replace(replacement, log)
    assert texts(t) == ["brand new file, longer than the previous one!!"]


def test_missing_file_then_created(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    t = LogTailer(log)
    assert texts(t) == []
    log.write_text("first\n")
    assert texts(t) == ["first"]


def test_invalid_utf8_is_replaced(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_bytes(b"ok \xff\n")
    assert texts(LogTailer(log, start_offset=0)) == ["ok �"]


def test_max_read_chunks(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text("".join(f"line{i}\n" for i in range(100)))
    t = LogTailer(log, start_offset=0, max_read=64)
    seen: list[str] = []
    while batch := texts(t):
        seen.extend(batch)
    assert seen == [f"line{i}" for i in range(100)]


def test_find_last_line_offset(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_bytes(b"a\nGen 1\nb\nGen 2\nc\n")
    off = find_last_line_offset(log, b"Gen ")
    assert off == len("a\nGen 1\nb\n")
    assert find_last_line_offset(log, b"nope") is None


def test_head_fingerprint(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_text("2024/01/01 00:00:00 ***** LOG FILE OPENING *****\n")
    assert head_fingerprint(log) == head_fingerprint(log)
    assert head_fingerprint(tmp_path / "missing") is None


def test_crlf_lines(tmp_path: Path) -> None:
    log = tmp_path / "Client.txt"
    log.write_bytes(b"one\r\ntwo\r\n")
    lines = LogTailer(log, start_offset=0).poll()
    assert [(line.text, line.end_offset) for line in lines] == [("one", 5), ("two", 10)]
