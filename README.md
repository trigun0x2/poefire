# poefire

A Path of Exile 1 farming tracker (map timing, loot, profit/hour), in the spirit of the
Torchlight: Infinite "YiHuo ETor" tracker.

**ToS-safe by design:** it only reads the game's `Client.txt` log and (from Phase 2) uses GGG's
official OAuth API. No memory reading, packet sniffing or input automation.

## Status

- [x] Phase 1: log parser and map-run tracker (CLI)
- [ ] Phase 2: loot via stash/inventory snapshots (OAuth API)
- [ ] Phase 3: poe.ninja pricing and web UI

## Usage

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run poefire paths                  # show the resolved Client.txt and database paths
uv run poefire track                  # tail Client.txt live and record runs
uv run poefire runs                   # list recorded runs
uv run poefire replay path/to/Client.txt   # parse a whole log into a throwaway DB, list runs
```

`--log`, `--db` and `--config` work with every command. Without `--log`, the tracker uses
`log_path` from the config, or else the first of these that exists:

- `C:\Program Files (x86)\Grinding Gear Games\Path of Exile\logs\Client.txt`
- `C:\Program Files (x86)\Steam\steamapps\common\Path of Exile\logs\Client.txt`
- `C:\Program Files\Steam\steamapps\common\Path of Exile\logs\Client.txt`

The config file is `%APPDATA%\poefire\config.toml` (`~/.config/poefire/config.toml` elsewhere):

```toml
log_path = 'D:\Games\Path of Exile\logs\Client.txt'
characters = ["MyWitch"]      # only count these characters' deaths (empty = count all)
extra_map_prefixes = []       # extra area-ID prefixes that start a run, e.g. ["Heist"]
extra_town_ids = []           # extra area IDs that pause a run
excluded_area_ids = []        # IDs matching a map prefix that should not start a run
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
  cli.py      `poefire` command
```

### Events

| Event | Log line |
| --- | --- |
| `LogOpened` | `***** LOG FILE OPENING *****` |
| `AreaGenerated(area_level, area_id, seed)` | `Generating level 83 area "MapWorldsStrand" with seed 123` |
| `ZoneEntered(zone)` | `: You have entered Strand.` |
| `PlayerSlain(character)` | `: Name has been slain.` |
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
  client restarts (`LOG FILE OPENING`).
- Time is counted from `You have entered` until the next `Generating` line, so loading
  screens are excluded. A run's `ended_at` is when you last left it.

### Persistence

SQLite tables: `runs`, `run_segments` (active intervals), `events` (every parsed event, tagged
with the run it happened in), and `kv` (log cursor and tracker state).

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

## Development

```sh
uv run pytest
uv run mypy
uv run ruff check src tests && uv run ruff format --check src tests
```
