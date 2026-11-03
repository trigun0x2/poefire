"""Windows-only fix-ups for the packaged app."""

from __future__ import annotations

import os
from pathlib import Path


def unblock_dlls(root: Path) -> int:
    """Remove the "downloaded from the internet" mark (the Zone.Identifier stream) from DLLs
    under `root`. Files extracted from a downloaded zip carry it, and .NET Framework then
    refuses to load them, which breaks the native window (pythonnet: "Failed to resolve
    Python.Runtime.Loader.Initialize"). Returns how many marks were removed."""
    removed = 0
    for dll in root.rglob("*.dll"):
        try:
            os.remove(f"{dll}:Zone.Identifier")
        except OSError:
            continue  # no mark (the usual case), or not on NTFS
        removed += 1
    return removed
