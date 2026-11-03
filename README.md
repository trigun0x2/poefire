# poefire

A Path of Exile 1 farming tracker: map timing, loot and value per map, and your stash's value
over time.

**No game hooks:** poefire reads the game's `Client.txt` log and your own account's
inventory and stash tabs, through the website with your POESESSID or through GGG's OAuth API.
It does no memory reading, packet sniffing or input automation.

## Status

- [x] Phase 1: log parser and map-run tracker (CLI)
- [x] Phase 2: loot via stash/inventory snapshots (OAuth API)
- [x] Phase 3: poe.ninja pricing and web UI

## Windows download

Download `poefire-<version>-windows-x64.zip` from the latest
[release](https://github.com/trigun0x2/poefire/releases/latest) and unzip it into a folder you
own, for example `%LOCALAPPDATA%\Programs\poefire`. Avoid `Program Files`: the updater needs
to write there.

- `poefire-ui.exe` opens the tracker in its own window. It logs to `%APPDATA%\poefire\poefire.log`.
- `poefire.exe` is the command line, for example `poefire.exe set-session` or `poefire.exe runs`.
  Run with no arguments, it opens the window too.

**Updates.** poefire checks GitHub for a new release on start and every 6 hours, and shows a
banner when one is out. **Install and restart** downloads the zip, checks it against its
published SHA-256, unpacks it, and closes poefire. A small script then copies the new files
over the old ones and starts poefire again. Your settings, cookie and history live in
`%APPDATA%\poefire` and aren't touched. The Updates section in Settings turns automatic checks
on or off and switches between the stable and beta (pre-release) channels. Versions before
v0.6.0 have no updater, so install v0.6.0 by hand once.

The build isn't code-signed, so Windows SmartScreen may warn on first launch ("More info" →
"Run anyway"). The window uses Microsoft Edge WebView2, which Windows 10 and 11 normally have.
Without it, poefire opens in your browser instead.

## Usage

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

# Loot tracking
auth = "session"                     # "session" (POESESSID, default) or "oauth"
account_name = "Exile#1234"          # full account name, with the #1234 part
league = "Keepers"
character = "MyWitch"                # default: the account's current character in the league
stash_tabs = ["Dump", "Currency"]    # tab names or ids to include in snapshots
snapshot_delay = 3.0                 # seconds to wait after reaching the hideout
snapshot_on_run_start = true         # also snapshot on map entry (see below)
# realm = "pc"                       # or "xbox" / "sony"
# contact = "you@example.com"        # optional; added to the User-Agent
# For auth = "oauth" only (needs an app registered with GGG):
# oauth_client_id = "yourclientid"
# oauth_redirect_uri = "http://127.0.0.1:8462/oauth/callback"   # must match your app
```

## Loot tracking

There are two ways to read your inventory and stash:

- **`auth = "session"` (default).** Uses your POESESSID cookie with the endpoints the
  pathofexile.com website itself uses (`character-window/get-stash-items`, `get-items`,
  `get-characters`, and `/api/stash` for map and unique tab sections). Use this while GGG
  isn't accepting new OAuth apps.
- **`auth = "oauth"`.** GGG's official developer API with OAuth 2.1 (authorization code +
  PKCE, public client, `account:stashes` and `account:characters` scopes). Needs an app
  registered with GGG (`oauth@grindinggear.com`). Set `oauth_client_id` and `contact`, then
  run `poefire login`.

**Session setup.** Log in on pathofexile.com in your browser and copy the `POESESSID` cookie
(dev tools → Storage/Application → Cookies). Set `account_name` in `config.toml` to your full
account name, including the `#1234` part. Then run `poefire set-session` and paste the cookie
at the hidden prompt; it checks the cookie by listing your characters. The cookie is saved in
`session.json` next to the database, readable only by you, and never in `config.toml`. You can
use the `POE_SESSID` environment variable instead. When you log out of the website or the
session expires, you'll get an HTTP 401: set a new cookie.

**How long a POESESSID lasts.** GGG decides that, not poefire: a cookie usually keeps working
until you log out of pathofexile.com, change your password, or GGG ends the session. poefire
measures it instead of guessing. Settings shows when the current cookie was saved, when it last
worked and how long it has worked, plus how long your previous cookies lasted. When the website
rejects the cookie (HTTP 401/403, or a redirect to the login page), loot tracking pauses. A
banner and the sidebar then say it expired, and how long it worked, and point you to Settings.

**What the cookie means.** POESESSID is your whole logged-in website session, not a
read-only key. Anyone who has it can act as you on pathofexile.com, so never share it or
paste it into chats or screenshots. poefire keeps it out of every log line and error message.
These endpoints aren't the documented developer API, so GGG can change or restrict them at
any time.

**Rate limits.** The website endpoints have tight per-IP limits. poefire waits at least 1.5s
between requests, follows the rate-limit headers, and never retries a 429: it skips that
snapshot and waits out `Retry-After` before the next one. poefire also identifies itself in
its User-Agent instead of posing as a browser. If GGG's edge protection starts rejecting it,
that's their call.

**How it works.** Each snapshot holds your character's inventory plus the watched stash tabs.
Equipped gear is not included. Snapshots form one chain, and each change is labelled by what
triggered the snapshot that closed it:

| Snapshot taken when | Change since the previous snapshot is |
| --- | --- |
| you return to hideout/town from a map | that map's **loot** |
| you enter a new map | hideout activity before it (trades, crafting, the map used) |
| `poefire snapshot` | a manual change, not attached to a run |

The snapshot on map entry keeps trading and crafting out of the loot figures. Several hideout
trips during one map add up into that map's loot.

Stackable items are compared by total quantity per item kind, so stack merges and splits don't
count as changes. Other items are compared by their unique item id, so moving loot from your
inventory into a watched tab nets to zero.

**Caveats**

- Watch every tab your loot lands in, including ones that Ctrl+click affinities send it to.
  Moving an item into an unwatched tab counts as losing it.
- Every watched tab costs one API request per snapshot. Special tabs (map, unique) cost one
  request per section.
- The API shows current state only. Map runs replayed from the log after a restart get no
  snapshots, and a snapshot more than 2 hours after the previous one starts a fresh baseline.
- A failed snapshot (rate limit, network) is skipped. The next good snapshot then covers both
  periods.

**Rate limits** are read from the `X-Rate-Limit-*` headers on every response, never
hard-coded. poefire keeps one hit of headroom under each window, counts hits made by other
tools on the same account, and on HTTP 429 waits for `Retry-After`.

## Campaign tracker

The **Campaign** tab times campaign runs from `Client.txt`, the way a LiveSplit auto-splitter does
(based on [POE-LiveSplit-Component](https://github.com/brandondong/POE-LiveSplit-Component)).

- **Start new run** starts the timer (optionally for a named character; otherwise the first
  character that levels up). Starting again stops the run in progress. **Finish** / **Stop** end
  it by hand; entering the epilogue after act 10 finishes it automatically.
- **Splits:** every completed act (the first time you enter an area of a later act) and the first
  visit to each zone. Acts are read from the area id (`1_1_1`, `2_6_town`...), so hideouts, the
  labyrinth and maps never count as a new act.
- **Times:** real time and a load-removed time. Loading screens are timed with the client's
  millisecond counter, from `Got Instance Details` to `You have entered ...`.
- **Class:** from the level-up lines (`Name (Class) is now level N`), so it shows the
  ascendancy once you've ascended. Level-ups and deaths are recorded too.
- Each act split shows the difference to your best finished run (PB). Every run is kept with its
  notes, which you can edit at any time; past runs can be deleted.

## Settings

Click **Settings** in the app's top bar to set up everything without editing files:

- **Game log:** where `Client.txt` is. You can paste the game folder instead, or leave it empty
  to detect it automatically.
- **Account:** your account name and your POESESSID. The cookie is write-only: the page shows
  whether one is saved, but never displays it again. **Check connection** lists your
  characters and stash tabs.
- **Loot tracking:** league, character, the stash tabs to watch (chosen from your real tab
  list), the map-start snapshot, the hideout snapshot delay, and whose deaths count. Death
  names match regardless of capitalization. The page lists the character names seen in your
  log and whether their deaths count, and it warns about a name that never appears in your log
  (an account name, for example).

Changes apply immediately, with no restart. They're saved to `config.toml` in the data folder
(`%APPDATA%\poefire`), which you can also edit by hand. Saving from the page rewrites the
file, so any comments in it are lost.

### Private leagues

Enter the private league's full name, for example `Fire Party (PL12345)`, either in Settings
or with "Other league…" in the top bar's league picker. After **Check connection**, Settings
also offers the leagues your characters are in. Stash and inventory are read from the private
league as usual.

poe.ninja has no prices for private leagues, so **Prices from** decides where prices come
from. Left empty, it uses the current softcore challenge league. Set it to the league yours is
based on, for example the hardcore one for a hardcore private league. In `config.toml` this
is `price_league`.

## Prices

Prices come from poe.ninja's league data: the currency exchange for currency, plus the stash
overviews for fragments, cards, scarabs, essences, maps, uniques, gems and so on. They're
cached in the SQLite database, so a restart doesn't re-download them, and refreshed every
30 minutes. poe.ninja itself updates about every 15 minutes, so polling faster gains nothing.
If poe.ninja is unreachable, the last cached prices stay in use, and the next attempt waits
5 minutes. Click the price pill in the UI to refresh now.

Items are matched like this:

- **Currency, fragments, cards, scarabs and other stackables:** by name.
- **Maps:** by name and tier.
- **Gems:** by name, closest level and quality, and whether they're corrupted.

Loot is only known from snapshots, and snapshots happen in the hideout. While you're inside
a map there's no live loot value: the API can't see items until the server has saved them,
and polling it during the map would waste the rate limit. A map's value appears when you
return to the hideout. If you go back to the hideout during a map, what you've dumped so far
shows as "so far".

**Gear isn't loot.** Uniques (unique maps included) and normal, magic and rare equipment
(weapons, armour, jewellery, flasks, jewels) are ignored between maps, so picking up, selling
or vendoring gear doesn't change a map's value. Gear is recognized by the category in its icon
path, or by level requirements and sockets. Everything else is tracked: currency, fragments,
scarabs, essences, divination cards, gems, maps, breachstones, logbooks, beasts and so on.
Items poe.ninja has no price for, and items worth under 0.05c (they'd show as "0c"), are
hidden from item lists and left out of the totals; each list says how many it hid. Runs are
valued at current prices, so past runs change value as the market moves.

## UI

`poefire ui` runs everything in one process: the log tailer, loot snapshots and price
refreshes. It serves a local page at `http://127.0.0.1:8460`. The top bar holds the league
selector. The sidebar switches between three tabs and stays visible from any tab:

- **Now:** the current map, elapsed time, level and deaths.
- **Recent stats:** the last map's loot, your average per map (last 10), maps this session and
  your total value.
- **Snapshot now**, and the status of prices and the last snapshot. Click either status line
  to refresh it.

**Session**
- the last map, front and center: loot value, time, deaths, how it compares with your average
  map, and its top drops (it updates as soon as you're back in the hideout)
- average chaos (and divine) per map over the last 10 finished maps that have loot data
- the session (runs less than 30 minutes apart): total loot, best run, map time, deaths
- the run history, and each run's loot, including what was spent in the hideout before it

**Overview**
- the total value of your watched tabs and inventory over time, in divine (default) or chaos,
  for the last 24 hours, 7 days, 30 days or all time. Hover the chart for exact values, or
  open the data table. Every complete snapshot records a value at that moment's prices, so
  the chart fills in as you play.
- the most valuable items from the latest snapshot at current prices, with quantity, price
  each, total and share. You can filter by tab or inventory.

**Settings** (see above)

The server only listens on localhost, rejects requests addressed to any other host name
(blocking DNS rebinding), and requires a custom header on state-changing requests, so other
websites can't trigger actions through your browser.

`--window` opens the same UI in a native window through pywebview, which uses Edge WebView2 on
Windows. Install it with `uv sync --extra window`. The window has no Windows title bar: drag
it by the app's top bar, double-click the bar to maximize, and resize it from the right or
bottom edge. Minimize, maximize and close are at the top right. Maximize leaves the taskbar
visible.

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
tagged with run and kind), and `price_cache` (poe.ninja tables per league).

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
- **Push to `dev`:** a pre-release, `v<next>-dev.<run number>`. Only installs on the beta
  update channel see it, and only the 10 newest pre-releases are kept.
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
