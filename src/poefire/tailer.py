"""Polling tailer for Client.txt.

The file is opened, read and closed on every poll instead of holding a handle: on Windows an
open handle without FILE_SHARE_DELETE would stop the user deleting or renaming the log while the
tracker runs. A poll with no new data is a single stat() call.

Offsets always sit on line boundaries: a trailing partial line is left unread until its newline
arrives, so the stored offset can be persisted and resumed exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TailLine:
    text: str
    end_offset: int  # byte offset just past this line's newline


class LogTailer:
    def __init__(
        self,
        path: Path,
        start_offset: int | None = None,
        max_read: int = 4 * 1024 * 1024,
    ) -> None:
        """`start_offset=None` starts at the end of the current file."""
        self.path = path
        self.max_read = max_read
        self._identity: tuple[int, int] | None = None
        st = self._stat()
        if st is None:
            self._offset = 0
        else:
            self._identity = (st.st_dev, st.st_ino)
            self._offset = st.st_size if start_offset is None else min(start_offset, st.st_size)
        self.resets = 0  # truncations/replacements seen, for diagnostics

    @property
    def offset(self) -> int:
        return self._offset

    def _stat(self) -> os.stat_result | None:
        try:
            return os.stat(self.path)
        except (FileNotFoundError, NotADirectoryError):
            return None

    def poll(self) -> list[TailLine]:
        """Return complete lines appended since the last poll (at most `max_read` bytes)."""
        st = self._stat()
        if st is None:
            return []
        identity = (st.st_dev, st.st_ino)
        if self._identity is not None and identity != self._identity:
            self._offset = 0  # file replaced
            self.resets += 1
        elif st.st_size < self._offset:
            self._offset = 0  # truncated
            self.resets += 1
        self._identity = identity
        if st.st_size == self._offset:
            return []

        try:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read(self.max_read)
        except OSError:
            return []  # transient (e.g. locked mid-rotation); retry next poll

        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            if len(chunk) < self.max_read:
                return []  # partial line, wait for the rest
            last_nl = len(chunk) - 1  # pathological line longer than max_read
        data = chunk[: last_nl + 1]

        lines: list[TailLine] = []
        pos = self._offset
        for raw in data.splitlines(keepends=True):
            pos += len(raw)
            lines.append(TailLine(raw.decode("utf-8", errors="replace").rstrip("\r\n"), pos))
        self._offset = pos
        return lines


def find_last_line_offset(path: Path, needle: bytes, window: int = 1024 * 1024) -> int | None:
    """Byte offset of the start of the last line containing `needle` within the final `window`
    bytes of the file, or None."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            start = max(0, size - window)
            f.seek(start)
            chunk = f.read()
    except OSError:
        return None
    idx = chunk.rfind(needle)
    if idx == -1:
        return None
    line_start = chunk.rfind(b"\n", 0, idx) + 1
    if line_start == 0 and start > 0:
        return None  # line began before the window
    return start + line_start


def head_fingerprint(path: Path, n: int = 256) -> str | None:
    """Identifies a log file across restarts (its first line holds the creation date)."""
    try:
        with open(path, "rb") as f:
            return f.read(n).hex()
    except OSError:
        return None
