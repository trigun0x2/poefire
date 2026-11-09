"""Release notes from the commits since the last public release (what the app shows under
"What's new" in its update banner).

    python3 scripts/release_notes.py [--head <commit>] [--repo owner/name]

Lists each commit's subject (the first line), newest first, leaving out the bots' own
commits (screenshots, tree data) and merges. Standard library only (runs on any runner).
"""

from __future__ import annotations

import argparse
import re
import subprocess

# commits that aren't changes to the app
SKIP = re.compile(r"^(README screenshots|PoB tree data:|Merge |CI: )")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def last_public_tag(head: str) -> str | None:
    """The newest vX.Y.Z tag reachable from `head`, other than one on `head` itself."""
    tags = git("tag", "--merged", head, "--list", "v[0-9]*.[0-9]*.[0-9]*", "--sort=-v:refname")
    on_head = set(git("tag", "--points-at", head).split())
    for tag in tags.split():
        if "-" not in tag and tag not in on_head:
            return tag
    return None


def subjects(since: str | None, head: str) -> list[str]:
    span = f"{since}..{head}" if since else head
    out = git("log", "--no-merges", "--format=%s", span)
    return [s.strip() for s in out.splitlines() if s.strip() and not SKIP.match(s.strip())]


def notes(head: str, repo: str | None) -> str:
    since = last_public_tag(head)
    changes = subjects(since, head)
    lines = ["What's new:", ""]
    lines += [f"- {s}" for s in changes] or ["- Small fixes and improvements."]
    if since and repo:
        lines += ["", f"All changes: https://github.com/{repo}/compare/{since}...{head[:12]}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--repo", help="owner/name, for the compare link")
    args = ap.parse_args()
    print(notes(args.head, args.repo), end="")


if __name__ == "__main__":
    main()
