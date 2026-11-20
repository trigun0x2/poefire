# Scope: price checking in poefire (an Awakened PoE Trade replacement)

Status: **scoping** (branch `price-check`). Nothing here is built yet.

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

poefire reads files and website data that are yours, listens to global hotkeys, and **never
sends input to the game**. APT crosses that line in several places: it presses Ctrl+C for you,
types chat commands, sends arrow keys for stash scrolling, and captures the screen for OCR.
A replacement keeps poefire's rules:

| APT feature | poefire's version |
| --- | --- |
| Ctrl+D presses Ctrl+C for you | **You press Ctrl+C** on the item (the game copies its text: a normal game feature). poefire sees item text arrive on the clipboard and opens the price check. Optionally only while a "price check" hotkey is held, or with a hotkey that opens the last copied item. |
| Chat commands, stash scrolling | **Not built** (input automation). |
| Heist OCR | **Not built** (screen reading). |
| Trade site searches | **Built, user-initiated only**: one search when you ask, never in the background; follows the trade site's rate-limit headers; identifies itself honestly (no browser disguise). |
| poeprices.info | Optional, off by default (a third party; the item text is sent to it). |

## How it would fit in poefire

- **Clipboard watcher (Windows):** `AddClipboardFormatListener` on a hidden message window (ctypes,
  like the overlay), on its own thread. Only text that looks like a PoE item ("Item Class:"
  first line, `--------` separators) is used; anything else is ignored and never stored.
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
| 1 | Clipboard watcher + item parser + poe.ninja prices | 3–4 days | Ctrl+C on a currency, card, unique, gem or map shows its price in a panel |
| 2 | Price-check window (interactive, near the cursor) | 2–3 days | The panel opens at the cursor, closes on Esc/click-away, never takes focus from the game unless clicked |
| 3 | Trade search for rares: mod filters, results list, link to the trade site | 1–2 weeks | Common rare bases (jewellery, armour, weapons, jewels) price with sensible default filters |
| 4 | Pseudo stats, item level/influence/sockets filters, bulk (currency) search | 1 week | Parity with APT for the common cases |
| 5 | Map check (dangerous mods you pick) | 2 days | Ctrl+C on a map highlights your bad mods |
| — | Not planned | — | Chat commands, stash scrolling, OCR (input automation / screen reading) |

## Open questions

1. Trigger: open on **any** item copy (Ctrl+C), or only while a hotkey is held / with a separate
   "price check last copied item" hotkey?
2. Rares: trade search only, or also poeprices.info estimates (sends item text to a third party)?
3. Mod data: GGG's trade data (recommended), or reuse APT's data files with credit?
4. Where results go: the new popup only, or also a "Price checks" history page in the app?
