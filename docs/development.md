# Developing poefire

## Running from source

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run poefire ui                     # the app: tracker + UI in your browser
uv run poefire ui --window            # same, in a native window (uv sync --extra window first)
uv run poefire paths                  # show the resolved Client.txt and database paths
uv run poefire track                  # tail Client.txt live and record runs
uv run poefire runs                   # list recorded runs
uv run poefire replay path/to/Client.txt   # parse a whole log into a throwaway DB, list runs
uv run poefire set-session            # save your POESESSID cookie (hidden prompt)
uv run poefire tabs                   # list stash tabs, to choose which ones to watch
uv run poefire snapshot               # take a snapshot now and show what changed
uv run poefire loot                   # loot per recent run
```

## Design

```
src/poefire/
  events.py   typed log events (frozen dataclasses)
  parser.py   Client.txt line -> event (pure)
  zones.py    area ID -> HIDEOUT / TOWN / MAP / OTHER
  tracker.py  run state machine (pure, JSON-serializable state)
  tailer.py   polling file tailer
  store.py    SQLite persistence
  service.py  tailer -> parser -> tracker -> store
  config.py   TOML config and default paths
  api/        POESESSID client, OAuth (PKCE + loopback redirect), rate limiter, API client
  loot/       item identity, snapshots, diffing, run attribution (recorder)
  pricing/    poe.ninja client, price matching, cached refresh service, valuation
  web/        runtime (background threads), JSON views, FastAPI app, static front end
  wiring.py   builds API/loot objects from the config
  cli.py      `poefire` command
```

### Events

| Event | Log line |
| --- | --- |
| `LogOpened` | `***** LOG FILE OPENING *****` |
| `AreaGenerated(area_level, area_id, seed)` | `Generating level 83 area "MapWorldsStrand" with seed 123` |
| `ZoneEntered(zone)` | `: You have entered Strand.` |
| `PlayerSlain(character, suicide)` | `: Name has been slain.` / `: Name has committed suicide.` |
| `TradeAccepted` | `: Trade accepted.` |
| `ClientClosed` | `Closing game gracefully` |
| `LevelUp(character, character_class, level)` | `: Name (Witch) is now level 92` |
| `Whisper(direction, character, guild, message)` | `@From <GUILD> Name: msg` / `@To Name: msg` |
| `StatusModeChanged(mode, enabled, autoreply)` | `: AFK mode is now ON. Autoreply "..."` / `... OFF.` (also DND) |

Every event carries `ts`, the client's local time as logged. System messages must carry the
`: ` prefix, so chat lines like `Bob: You have entered Hideout.` can't trigger them.

### Run rules

- Entering a map instance (area ID + seed) that isn't the current run's starts a new run.
- Hideouts and towns pause the run. Coming back to the same seed resumes it.
- Areas reached directly from the run (Vaal side areas, Abyssal Depths, and so on) count as run
  time. Maps can only be opened from a hideout or town, so even a map-like area ID reached from
  inside the run counts as a side area.
- The run finishes when a different map instance is generated after leaving it, or when the
  client closes (`Closing game gracefully`) or restarts (`LOG FILE OPENING`).
- Time is counted from `You have entered` until the next `Generating` line, so loading
  screens are excluded. A run's `ended_at` is when you last left it.

### Persistence

SQLite tables: `runs`, `run_segments` (active intervals), `events` (every parsed event, tagged
with the run it happened in), `kv` (log cursor and tracker state), `snapshots` and
`snapshot_items` (only the latest snapshots keep their items), `loot` (per-item changes,
tagged with run and kind), and `price_cache` (poe.ninja tables per league). Log events older
than 90 days are deleted once a day; nothing reads them.

Each poll commits its events, run updates, byte offset and tracker state in one transaction.
On restart, the tracker resumes from the saved offset if the log file is unchanged, so
anything logged while it was closed is caught up. Otherwise (first run, or the log was
replaced) it finishes any stale run and starts from the last `Generating` line near the end of
the file, which picks up a map that's already in progress.

### Tailer

The tailer opens, reads and closes the file on every poll instead of keeping it open. On
Windows, a held handle would block the user from deleting or renaming `Client.txt`. An idle poll
is one `stat()` call. Offsets always land on line boundaries, and a partial last line waits for
its newline. A smaller file or a new file identity means the log was truncated or replaced, and
reading restarts from byte 0.

## Releases

`.github/workflows/ci.yml` runs lint, type checks and tests on Windows and Linux for every push
and pull request.

`.github/workflows/release.yml` builds and publishes the Windows app:

- **Push to `master`:** a public release. The workflow takes the latest public `vX.Y.Z` tag
  and bumps the minor version (v0.6.0, v0.7.0…), and the release is marked as GitHub's latest.
  This is what the auto-updater installs.
- **Push to `dev`:** replaces the single rolling pre-release, tagged `beta` (its zip carries the
  version, `<next>-dev.<run number>`). Only installs on the beta update channel see it, and the
  Releases page shows just this one beta entry next to the public versions.
- **Manual run from the Actions tab:** builds the app as a workflow artifact only.

Each build stamps the version into the app, runs the tests, and builds a one-folder app with
PyInstaller (`packaging/windows/poefire.spec`). It then starts the built exe to check it serves
the UI and that the native window opens, and publishes `poefire-<version>-windows-x64.zip`
(app files at the top level) with its SHA-256 checksum. Releases run one at a time, so two
quick pushes get two versions.

## Development

Day-to-day work happens on `dev`; merging into `master` (the default branch) publishes a
public release.

```sh
uv run pytest
uv run mypy
uv run ruff check src tests && uv run ruff format --check src tests
```

### README screenshots

```sh
uv run python scripts/demo_data.py /tmp/demo/poefire          # made-up demo data
XDG_CONFIG_HOME=/tmp/demo POE_SESSID=demo uv run poefire --log /tmp/demo/Client.txt ui --no-browser --port 8473
node scripts/screenshots.js http://127.0.0.1:8473 docs/images  # needs Playwright
uv run python scripts/overlay_preview.py docs/images/overlay.png
```
