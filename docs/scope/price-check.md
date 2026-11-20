# Scope: price checking in poefire (an Awakened PoE Trade replacement)

Status: **building**. Done: in-game shortcuts (step 1), Ctrl+D copy + parser (step 2), the
popup (step 3) and the trade search (step 4, first version).

## Decided (Nov 2026)

- **Build only:** the price check, preset chat commands (zone shortcuts) and saved stash/vendor
  search strings. **Not** the wiki links, heist OCR, stash scrolling, map check or widgets.
- **Input rule relaxed to "one key press = one action"** (was "never send input to the game"):
  a hotkey you press may send one action to the game (Ctrl+C to copy the item under the cursor;
  type one chat command and press Enter; paste one search string). Never repeated, timed, chained
  across server actions, or done without a key press. README and SECURITY.md say so.
- **Rares:** trade search only (no poeprices.info; nothing leaves your PC except to GGG).
- **Chat commands built in:** /hideout, /exit, and zone shortcuts (/kingsmarch, /menagerie and
  the other zone commands the game has), each on its own hotkey, changeable or off.
- **"Search":** saved stash/vendor search strings (e.g. map-mod regex): a hotkey types one
  into the in-game search box you have open.

### Decided (Nov 20)

- **Every item except bulk ones is priced on the trade site**, in a popup beside the cursor:
  the item's mods as filters (explicit, crafted and fractured ticked at ≥90% of the roll;
  implicits, enchants and item level unticked; min/max editable), searched when the popup
  opens and again with **Search**. Shows the **5 cheapest** listings and the **average of the
  20 cheapest without the cheapest and dearest two**, plus a link to the search on the site.
- **Bulk items** (currency, fragments, scarabs, cards, essences, anything with a stack size):
  poe.ninja's price, and a note that they're traded on the in-game Currency Exchange.
- Mod wording → trade stat ids from GGG's `/api/trade/data/stats` (`/items`, `/static` for
  bases and currency names), fetched on first use and kept for a day (`trade-data.json`).
- Trade requests never wait out a rate limit: the popup says how long to wait instead.

## What Awakened PoE Trade does

[Awakened PoE Trade](https://github.com/SnosMe/awakened-poe-trade) (APT, MIT licence, Electron,
TypeScript + Vue) is the overlay most players use to price items. From its source (Nov 2026):

| Feature | How APT does it | Size (lines) |
| --- | --- | --- |
| **Price check** (Ctrl+D) | Presses Ctrl+C in the game for you, reads the item text from the clipboard, parses it, and opens a panel at the cursor with the item's mods as filters. Searches the official trade site (`/api/trade/search` then `/api/trade/fetch`) and lists the cheapest listings; bulk exchange for currency. | parser 2,500; price check 9,200 (filters 4,800, trade 2,500) |
| Price for uniques, gems, currency | poe.ninja | (in the above) |
| Price for rares | poeprices.info (a third-party ML estimate) | small |
| Mod → trade filter mapping | `stats.ndjson` / `items.ndjson`: every mod text pattern and its trade stat id, generated from the game files (en: ~9,900 stats, ~4,700 items) | data |
| Map check | Highlights dangerous map mods you configure | 580 |
| Item check (Alt+W) | Opens the item on the wiki / poedb | 320 |
| Overlay widgets (Shift+Space) | Stopwatch, stash search, image strip... | 1,200 |
| Chat commands (F5 /hideout, F9 /exit) | Types the command into the game's chat | small |
| Stash scrolling (Ctrl+wheel) | Sends arrow keys to the game | small |
| Heist gem finder | Screen capture + OCR | — |

## The ToS line

GGG's rule for macros is one server action per key press. poefire follows it (decided above):

| Feature | What poefire does |
| --- | --- |
| Price check (Ctrl+D) | On your key press: sends one Ctrl+C to the game (it copies the item under the cursor), reads the item text from the clipboard, restores what was on the clipboard before, and opens the panel. |
| Chat commands (F5 /hideout ...) | On your key press: opens chat (Enter), types the one command, presses Enter. One command per press. |
| Saved search strings | On your key press: pastes the saved string into the search box you have open (Ctrl+F is yours to press first). |
| Trade site searches | Only when you ask (opening a price check, changing filters); never in the background; follows the trade site's rate-limit headers; identifies itself honestly (no browser disguise). |
| Not built | Stash scrolling, heist OCR, wiki/poedb links, map check, widgets, poeprices.info. |

Sending keys: Win32 `SendInput` (ctypes), only while the game window has focus (checked with
`GetForegroundWindow`, like the overlay), after waiting for the hotkey's own modifier keys to be
released so they don't mix with what's sent.

## How it would fit in poefire

- **Copying the item (Windows):** on Ctrl+D, save the clipboard, send Ctrl+C, wait for the
  clipboard to change (up to ~250 ms), read it, then put the old clipboard back. Only text that
  looks like a PoE item ("Item Class:" first line, `--------` separators) is used; nothing is
  stored or logged.
- **Item parser (Python):** port the needed part of APT's parser: header (class, rarity,
  name, base), properties (quality, sockets/links, item level, map tier), requirements, and mods
  with their type (implicit, explicit, crafted, fractured, enchant), plus corrupted/mirrored/
  unidentified flags. A few hundred lines for the common item types, not APT's 2,500 for all.
- **Mod → trade stat mapping:** two options:
  1. **GGG's own trade data** (`/api/trade/data/stats`, `/items`, `/static`): official, always
     current, no dependency on APT; we build the text matching ourselves.
  2. **APT's `stats.ndjson`/`items.ndjson`** (MIT, with credit): already handles the hard cases
     (ranges, "#% increased", alternatives, pseudo stats), but depends on APT updating it each
     league.
  Recommendation: start with GGG's data for the common mods, use APT's data as a reference.
- **Pricing:**
  - Currency, fragments, cards, uniques, gems, maps: **poe.ninja** (poefire already prices all
    of these): instant, no trade request.
  - Rares and other items with mods: a **trade search** with the filters you tick (defaults:
    the item's main mods at about 90% of their rolls), showing the 10 cheapest listings, how
    long they've been listed, and a link to open the search on the trade site.
- **UI:** the existing overlay is a picture (not clickable). The price check needs a small
  **interactive window** near the cursor: a second frameless, always-on-top pywebview window
  showing a new page (`/price-check`) of the same local web UI. Esc or clicking elsewhere closes
  it. The main app window stays as it is.
- **Rate limits:** the trade site's limits are much tighter than the stash's, and penalties last
  minutes. A separate limiter (bucket) for trade requests that never blocks loot snapshots
  (as done for the trade history), and the panel shows the wait instead of queueing silently.

## Risks to check first (a spike, before building)

1. **Can poefire reach the trade site at all?** Cloudflare may challenge a non-browser client;
   APT runs inside Chromium (Electron), which passes. A one-day spike from a real Windows PC:
   search + fetch with httpx and an honest User-Agent, with and without POESESSID. If it's
   challenged, the fallback is to run the search inside the app's WebView2 window (a real
   browser engine), as APT effectively does.
2. **GGG's position on third-party trade tools.** The trade API isn't documented and GGG has
   said access is controlled ("whitelisting"). APT is widely used and tolerated; poefire would do
   the same thing on the same terms. Worth a short email to GGG once a prototype exists.
3. **Mod matching quality.** Parsing is easy; matching every mod wording to its trade stat id
   (and pseudo totals like "+# total to Elemental Resistances") is where APT's 4,800 lines of
   filter code went. Start with the 200 most common mods and grow.
4. **Clipboard access** is personal data: only item text is ever read, nothing is stored or
   logged, and the watcher only runs while the feature is on.

## Milestones

| # | Milestone | Size | Done when |
| --- | --- | --- | --- |
| 0 | Spike: trade search from Python on Windows (Cloudflare, rate limits) | 1 day | A search + fetch works, or we know we need WebView2 |
| 1 | Key sending (SendInput, game-focus check) + chat commands + saved search strings, with Settings | 2–3 days | F5 takes you to your hideout; a search hotkey fills the stash search |
| 2 | Ctrl+D copy + item parser + poe.ninja prices | 3–4 days | Ctrl+D on a currency, card, unique, gem or map shows its price |
| 3 | Price-check window (interactive, near the cursor) | 2–3 days | The panel opens at the cursor, closes on Esc/click-away, never takes focus from the game unless clicked |
| 4 | Trade search for rares: mod filters, results list, link to the trade site | 1–2 weeks | Common rare bases (jewellery, armour, weapons, jewels) price with sensible default filters |
| 5 | Pseudo stats, item level/influence/sockets filters, bulk (currency) search | 1 week | Parity with APT for the common cases |
| — | Not planned | — | Wiki links, map check, widgets, stash scrolling, OCR, poeprices.info |

## Defaults (change them if you disagree)

- Hotkeys: Ctrl+D price check (closes when the cursor leaves it), Ctrl+Alt+D price check that
  stays open; F5 /hideout, F9 /exit; zone shortcuts and search strings off until you give them
  a key.
- Mod data: GGG's trade data, with APT's data files (MIT) as a reference.
- Results: the popup only (no history page).
