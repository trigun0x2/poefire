# poefire reference

Everything in detail. New here? Start with the [README](../README.md).

## Installing and updating

Download `poefire-<version>-windows-x64.zip` from the latest
[release](https://github.com/trigun0x2/poefire/releases/latest) and unzip it into a folder you
own, for example `%LOCALAPPDATA%\Programs\poefire`. Avoid `Program Files`: the updater needs
to write there.

- `poefire.exe` opens the tracker in its own window. It logs to `%APPDATA%\poefire\logs\poefire.log`.
- `poefire-cli.exe` is the command line, for example `poefire-cli.exe set-session` or
  `poefire-cli.exe runs`.
- Updating from v0.11.0 or earlier (where the app was `poefire-ui.exe`): install by hand once,
  since those versions can't install this layout themselves.
  Run with no arguments, it opens the window too.

**Updates.** poefire checks GitHub for a new release on start and every 6 hours, and shows a
banner when one is out. **Install and restart** downloads the zip, checks it against its
published SHA-256, unpacks it to `%LOCALAPPDATA%\poefire\updates`, and closes poefire. The
downloaded new version then copies itself over the old one and starts poefire again; every
step is logged to `%LOCALAPPDATA%\poefire\updates\update.log`. If an update didn't apply,
that log says why (a file still in use, for example). Your settings, cookie and history live in
`%APPDATA%\poefire` and aren't touched. The Updates section in Settings turns automatic checks
on or off and switches between the stable and beta (pre-release) channels. Versions before
v0.6.0 have no updater, so install v0.6.0 by hand once.

The build isn't code-signed, so Windows SmartScreen may warn on first launch ("More info" →
"Run anyway"). The window uses Microsoft Edge WebView2, which Windows 10 and 11 normally have.
Without it, poefire opens in your browser instead.

## Command line and config file

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
stash_tabs = ["Dump", "Currency"]    # tab names or ids: the Overview (and the 10-minute mode)
league_tabs = ["Keepers|Dump"]       # picked per league ("League|tab"; "League|" = none);
                                     # a league without entries uses stash_tabs
snapshot_delay = 3.0                 # seconds after a zone change before the inventory is read
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

**How a map's loot is measured** (the default, "per map"). Your stash can't change while
you're in a map (there's no stash there), only your inventory can. So poefire reads your
inventory as you go into a map and again when you're back in your hideout or town: what came
into it in between is the map's loot, wherever it goes afterwards (any tab, sold, used up).
That's two requests per map, however many tabs you watch. Going back into the same map after
a hideout trip is another pair of reads, and the map's loot adds up over its trips.

| Your inventory is read | For |
| --- | --- |
| as you go into a map (or back into it) | the starting point of that stay |
| when you're back in your hideout or town | that stay's loot: what came into the inventory |
| about every 30 minutes while you map, with the watched tabs | the Overview: what you hold, and its value over time |
| when you press Snapshot now, with the watched tabs | the same, at once |

The website shows your inventory as the game last saved your character, which it does at
every zone change. A read between two zone changes sees the inventory as it was when you
entered that zone, so it doesn't matter exactly when it's made, as long as you haven't
changed zones again: a read that finishes after you have could hold what you did since, and
is thrown away. A read that fails (network, rate limit) is tried again while you're still in
that zone, and one held up by the website's rate limit waits before it's sent (never by
sending a request that will be refused). If you leave the hideout again within a few seconds,
before your inventory could be read, the read as you go back into a map counts for both: a
few seconds aren't enough to change anything there. Otherwise that trip has no loot data
rather than a wrong number: the map counts the loot of its other trips, or shows no loot
data.

**The watched tabs** are read for the Overview, about every 30 minutes while you map, just
after a map starts: your stash can't change until you're back, so the tabs agree with each
other and with the inventory. Reading them stops (and what was read is dropped) if you're
back before they're all read, and waits when the website's rate limits are tight, so the
inventory reads always have room. Each watched tab costs one request; of the map tab, only
the Tier 16, Special (nightmare, Valdo, guardian maps...) and Unique sections are read, one
request each; the other tiers aren't. Unique tabs aren't read at all: of uniques only unique
maps count. The Overview shows a tab's sections as one chip (e.g. "Maps").
Changed in Settings, the watched tabs are read at the next map start. Settings lists the tabs
of each league your characters are in (a request per league when you press Refresh tabs) under
its own heading, and each league keeps its own picks: switching league watches that league's
tabs. Only the selected league's tabs are ever read. A league you haven't picked tabs for uses
the tabs picked before this existed (`stash_tabs`). Too many to read between two maps (about
70 requests) and they aren't read: Settings and the log say so.

**Tracking mode** (Settings → Loot tracking, `loot_mode` in config.toml):

- **Per map** (default, `per_map`): as above, each map has its own loot, from two inventory
  reads.
- **Every 10 minutes** (`interval`): your inventory and every watched tab are read every
  10 minutes while you're mapping (a map run in the last 30 minutes), plus once when you
  start mapping again after that, and not around maps. The change between two reads, over
  the inventory and all the tabs, counts towards the session, so hideout trading and crafting
  count too. The Session page then shows the session's total net loot, maps done, the average
  per map (total ÷ maps), loot per hour and what changed, with the recent maps listed
  without loot; per-map loot, the Maps table and the Sessions history are hidden. Every read
  costs a request per tab: by far the more expensive mode. A read waits when it doesn't fit
  in the website's rate limits. Changing the watched tabs reads them at once, so what's
  already in a new tab isn't counted.

**Trade sales aren't loot** (Settings → "Don't count trade sales as loot", off by default;
`trade_sales = true` in config.toml). Per map, a sale's payment never comes into your
inventory, so it never counts: the setting is for the 10-minute mode. There, with the
POESESSID connection, each read also fetches your trade history
(`/api/trade/history/<league>`, the sales from your merchant tabs, at most once a minute).
When a sale's payment shows up in a watched tab, at least the whole price in the currency it
was paid in, that amount is taken back out of the change it shows up in, as a minus line,
"Sold on trade · <item>", tagged *trade*. Sales from before poefire first read the history
are skipped. A sale whose payment never shows up in a watched tab is dropped after a week. If
the history can't be read (OAuth login, the site refusing it), sales count, and poefire
tries again after 30 minutes.

**Session setup.** Log in on pathofexile.com in your browser and copy the `POESESSID` cookie
(dev tools → Storage/Application → Cookies). Set `account_name` in `config.toml` to your full
account name, including the `#1234` part. Then run `poefire-cli.exe set-session` (from source:
`uv run poefire set-session`) and paste the cookie
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
A public profile's characters and equipment can be read without a login (its bags come back
empty), so only a stash read shows the cookie works: a refused stash list in a league your
characters are in means it expired (in another league, the league name is wrong). poefire
checks the cookie that way, one request at most every 10 minutes, on the first inventory read,
when your inventory comes back empty, and after a refusal, so an expired cookie isn't taken
for a map that dropped nothing.

**What the cookie means.** POESESSID is your whole logged-in website session, not a
read-only key. Anyone who has it can act as you on pathofexile.com, so never share it or
paste it into chats or screenshots. poefire keeps it out of every log line and error message.
These endpoints aren't the documented developer API, so GGG can change or restrict them at
any time.

**Rate limits.** The website allows about 100 inventory and stash reads per account every 30
minutes, and answers going over with 10 minutes of refusals (GGG also restricts apps that make
many refused requests). poefire reads the limits from the `X-Rate-Limit-*` headers on every
response, never hard-coded; keeps a tenth of every window free for the website itself and
other tools; counts hits the server reports from other tools on the same account; waits at
least 1.5s between requests; and never retries a 429: it waits out `Retry-After` first. What
it knows of the limits is kept across a restart, so a restarted app doesn't walk into a
penalty still in force. While a rate limit is in force, the sidebar, a banner and the in-game
overlay say so and when it ends. poefire also identifies itself in its User-Agent instead of
posing as a browser. If GGG's edge protection starts rejecting it, that's their call.

**Loot counts what you gained, not what you spent.** A map's (or a session's) loot is the value
of the items that went up. Items that went down (portal scrolls used in the map; in the
10-minute mode also the maps and currency used, and crafting) are listed, dimmed, but not
taken off, so loot is never negative. Trade sales' payments are the exception: when that
setting is on, they're taken back out.

Stackable items are compared by total quantity per item kind, so stack merges and splits don't
count as changes. Other items are compared by their unique item id, so moving an item between
your inventory and a watched tab nets to zero (in the 10-minute mode, where tabs count).

**Caveats**

- Per map, loot is what came into the inventory of the character poefire reads (Settings →
  Character, or the account's current one): map with that character.
- In the 10-minute mode, watch every tab your loot lands in, including ones that Ctrl+click
  affinities send it to: moving an item into an unwatched tab counts as losing it.
- Map prices from poe.ninja with fewer than 10 listings are left out (a few sellers' asking
  prices); a blighted map without its own price gets the cheapest blighted map of its tier.
- The API shows current state only. Map runs replayed from the log after a restart get no
  reads, and in the 10-minute mode a read more than 2 hours after the previous one starts a
  fresh baseline.

### Sessions

**New session** (Session page) starts counting from zero: the last map, the tiles, the average
per map, the runs table and the sidebar only cover maps from then on. A map you're inside at
that moment moves into the new session. Earlier sessions stay in the **Sessions** table with
their length, maps, average map time, average loot per map (finished maps) and total loot; click
one to look at its maps again, or delete it (with its maps) with ✕. On the first start with
sessions, the maps you played in the last sitting (no more than 30 minutes apart) form the
current session.

## Campaign tracker

The **Campaign** tab times campaign runs from `Client.txt`, the way a LiveSplit auto-splitter does
(based on [POE-LiveSplit-Component](https://github.com/brandondong/POE-LiveSplit-Component)).

- **Hotkey (default `Ctrl+Alt+R`):** works while the game has focus. Press it to start a run, then
  again to pause and resume the timer; a beep confirms (high: started/resumed, low: paused).
  Pick another key or turn it off under Settings → Campaign. poefire only listens for the key
  (Windows `RegisterHotKey`, like LiveSplit); it never sends input to the game. While paused, the
  timer and load time stand still.
- **Start new run** starts the timer (optionally for a named character; otherwise the first
  character that levels up). Starting again stops the run in progress. Entering the epilogue
  after act 10 finishes a run; **Stop** ends it early (it stays in the list, marked as stopped).
- **Splits:** every completed act (the first time you enter an area of a later act) and the first
  visit to each zone. Acts are read from the area id (`1_1_1`, `2_6_town`...), so hideouts, the
  labyrinth and maps never count as a new act.
- **Times:** real time and a load-removed time. Loading screens are timed with the client's
  millisecond counter, from `Got Instance Details` to `You have entered ...`.
- **Class:** from the level-up lines (`Name (Class) is now level N`), so it shows the
  ascendancy once you've ascended. Level-ups and deaths are recorded too.
- **Ranger mule:** a run that starts with a throwaway Ranger (level 2 at most, never past
  Lioneye's Watch) before the real character is made keeps its timer running and is tagged
  "muled". The mule's level-ups, zones and deaths are dropped, so the real character's first
  visits are the splits. If the "mule" keeps levelling, it was the real character after all and
  the tag is removed.
- Each act and zone split shows the difference to your PB: the best finished run of the same
  base class (Deadeye and Ranger runs count together). Next to it, **Best** is your fastest time
  at each split over every run of that class that got past Act 1 (finished or not); a gold
  segment is an act you've never done faster.
- **Top runs:** your 3 fastest finished runs, also marked 1st / 2nd / 3rd in the runs list.
- **Where you lost time:** the 3 zones of a run with the most time lost, measured from first
  entering a zone to reaching the next new one, against your fastest time in that zone (any
  run of the class past Act 1), else your PB's, else the run's longest zones. Each comes with
  its layout guide from
  [Cyclon's Advanced Campaign Guide](https://www.definitivguide.com/docs/PoE1/layout) by
  **CyclonDefinitiv** ([YouTube](https://www.youtube.com/@CyclonDefinitiv)): his layout
  pictures, points of interest and zone read, loaded from his site (kept for a week) and
  credited, with a link to his page. poefire ships none of his content.
- Every run is kept with its notes and the game patch it was played on (e.g. 3.27, filled in
  from the PoE wiki's patch list, editable); past runs can be deleted.

## In-game overlay

A small panel over the game (Windows app). It shows while Path of Exile is the active window and
lets clicks through to the game. Set the game to **Windowed** or **Windowed Fullscreen**: nothing
can be drawn over exclusive fullscreen.

- **Campaign run:** the timer, act and class, then your last 3 areas with your time, your best
  time there (over every run of the same base class that got past Act 1) and the difference.
- **Mapping:** the session's average chaos per map (finished maps with loot data, since the
  session started), the session's total and number of maps, and the timer of the map you're in.
- **Hotkeys:** `Ctrl+Alt+O` shows / hides it; `Ctrl+Alt+L` unlocks it to drag it somewhere else,
  press it again to lock it there (the position is remembered). The **Move / Lock** button in the
  overlay's top-right corner does the same: while locked, only that corner takes clicks, and
  only while the cursor is over it; everywhere else, clicks go to the game. Both, and an on/off switch, are
  under Settings → In-game overlay.

The overlay only reads which window is in front (by its title) and draws over the game; it
never sends input to the game.

## In-game shortcuts

Settings → In-game shortcuts: hotkeys that send **one action** to the game per key press
(GGG's rule for macros), only while Path of Exile is the window in front. Elsewhere the keys
do what they normally do (F5 still refreshes your browser): poefire registers them when the
game gets focus and lets them go when it loses it.

Every in-game shortcut is off until you give it a hotkey.

- **Chat commands:** open chat, paste the command, press Enter. Built in, without a key:
  /hideout, /exit, /kingsmarch, /menagerie and /delve. Give any of them a hotkey, change the
  command, remove them, or add your own (any one line of chat).
- **Stash and vendor searches:** with the stash or a vendor open, the hotkey focuses its
  search box (Ctrl+F) and pastes the saved text, e.g. a map-mod regex like `"!ele|!reg"`. Give
  each a name, a hotkey and up to 250 characters.
- **Regexes:** save as many named regexes as you like and pick one with its radio button; the
  regex hotkey (Ctrl+B by default, live only once a regex is picked) pastes the picked one
  into the open stash or vendor search box, the same way. Switch regexes by picking another and saving, no new hotkey needed.

The text goes in by pasting (the clipboard is put back afterwards), never by typing letters:
if chat didn't open, typed letters would land in the game as skill and panel keys. Hotkeys are
sent after you let go of the hotkey's own modifiers. All hotkeys must differ from each other
and from the campaign and overlay hotkeys; a letter or digit needs a modifier. In
config.toml: `chat_commands = ["F5|/hideout", "|/exit", ...]` (hotkey | command; no hotkey = off) and
`search_strings = ["Ctrl+Shift+1|Bad maps|!ele|!reg"]` (hotkey | name | text);
`regexes = ["Bad maps|!ele|!reg"]` (name | text), `regex_active = "Bad maps"` and
`regex_hotkey = "Ctrl+B"`.

## Build snapshots

The **Builds** tab keeps each character's build as it levels: at levels 5 and 10, then at every
level from 15. The website only sees gear and passive changes once the game saves them, on a
zone change, so a level is captured at your first zone change after reaching it (two levels in
one zone make one snapshot, labelled with the higher one).

From level 80, levels come slowly, so there are also **stale-level snapshots** (Settings →
Builds → "When not levelling (level 80+), also snapshot every (hours)": a whole number of
hours, 24 by default, 0 for never; `build_stale_hours` in config.toml). When the character
you're playing has had no snapshot for that long, it's captured at a zone change once you've
played for an hour that day, and **kept only if its gear, gems or passives changed** since
its last snapshot (gem levels don't count). A failed try is repeated an hour later and isn't
recorded as a missed level.

On the Builds tab, several snapshots at the same level show as dates to pick from, and each
snapshot shows what changed since the one before it: the changed gear is outlined, new gems
are ringed, and a line lists the slots, new gems and passive points.

- Any character on your account is captured; a party member's level-up is ignored. Lines read
  while catching up on the log after a restart never trigger a snapshot (the website only has
  the build as it is now).
- Each snapshot stores the website's item and passive-tree data, compressed. The tab shows a
  level slider with the gear, gem links and passive counts at each level; **Snapshot now**
  captures the current build on demand.
- **Copy PoB code** copies the level's build as a Path of Building code: in PoB, open
  *Import/Export Build* and paste it under *Import from code*. The code holds the gear (with
  sockets, links, influences and crafted/fractured/enchant mods), the gem links per item, the
  passive tree with masteries and tree jewels, and the bandit and pantheon choices. It's made
  the way PoB's own "Import from website" reads the same data (PoB recalculates everything on
  import). The passive tree data PoB needs (class ids, jewel sockets) is in
  `src/poefire/pob_tree.py`; run `uv run python scripts/update_pob_tree.py <version>` when a
  new league adds a tree version.
- A failed fetch is retried once after 3 minutes; then the level is marked as missed, with the
  reason. Uses the account connection set up for loot (same rate limits). Turn it off under
  Settings → Builds.

## Settings

Click **Settings** in the app's top bar to set up everything without editing files:

- **Game log:** where `Client.txt` is. You can paste the game folder instead, or leave it empty
  to detect it automatically.
- **Account:** your account name and your POESESSID. The cookie is write-only: the page shows
  whether one is saved, but never displays it again. **Check connection** lists your
  characters and stash tabs.
- **Loot tracking:** league, character, the stash tabs to watch (chosen from your real tab
  list) and what they cost in requests, the tracking mode, the wait after a zone change
  before your inventory is read, and whose deaths count. Death
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

**Maps are priced by their name.** A map's mods, rarity and a rare map's name don't matter:
"Oppressive Map of Rust (Tier 16)" and a rare "Gloom Hustle" are both a Map (Tier 16), at
poe.ninja's price for it; a Nightmare Map is a Nightmare Map. Corrupted and mirrored maps are
kept apart (listed as their own kind), at the same price, since poe.ninja has none for them.
A unique map is priced by its own name.

**Gear isn't loot.** Uniques (except unique maps) and normal, magic and rare equipment
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

## Troubleshooting

Settings > **Diagnostics** > **Save debug report** writes a `.zip` to
`%APPDATA%\poefire\reports` (and opens it in Explorer). Send that file: it has poefire's log,
the settings (never the POESESSID), the campaign runs with their splits, and the game's system
lines from the end of Client.txt (area changes, zone entries, level-ups, deaths; no chat or
whispers). Its `report.txt` starts with a summary: which Client.txt is read and whether lines
are coming in, what the log's last lines contain (with hints, e.g. a game not set to English),
and the campaign tracker's state. Turn on **Verbose logging** first to log why lines were
ignored. Without the app window: `poefire-cli.exe debug-report`.

To check a downloaded release zip against its signed build attestation:
`poefire-cli.exe verify poefire-<version>-windows-x64.zip` (with the `.sigstore.json` from the
same release next to it; `--beta` for a beta build). See [SECURITY.md](../SECURITY.md).

Logs: `%APPDATA%\poefire\logs\poefire.log` (rotated, 5 x 2 MB); the windowed app's own output
and crash tracebacks are in `console.log` next to it.

