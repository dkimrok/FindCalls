<h1 align="center">FindCalls</h1>

<p align="center">
  <b>One script that tracks academic calls for papers across five publisher ecosystems, merges them into a single sheet, and flags what's new since your last run.</b>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="Sources: 5" src="https://img.shields.io/badge/sources-5-1F4E79">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

Academic calls for papers (CFPs) live on publisher portals built on wildly different tech: static HTML, AJAX pagination, WordPress REST APIs, and aggressively bot-protected platforms. Checking them by hand is a weekly chore. **CFP Radar** handles all five in a single run — using the lightest technique that actually works for each — normalizes everything into one schema, and answers the only question that matters between runs: *what's new?*

Built by a defense operations-research analyst to stop manually refreshing a dozen journal pages. Initial index: **4,230 unique CFPs** across **5 sources**.

## Highlights

- **Single entry point.** `cfp_radar_all.py` runs every crawler and the merge. No other files needed.
- **Five sources, three fetch strategies** — pure REST, headed Playwright, and a Cloudflare-bypassing stealth browser — each matched to the site's actual defenses.
- **Diff on every run.** A first-seen registry means each run surfaces only newly posted calls, not the whole haystack.
- **Fault-isolated stages.** If one site is down or blocks you, that stage is skipped and the rest still produce output.
- **Relevance tagging.** Two editable regexes score each CFP (`★★` / `★`) against your target journals and topics.

## How it works

```
STAGE            SOURCE                       FETCH STRATEGY
─────────────────────────────────────────────────────────────────────────
tandf            Taylor & Francis             WordPress REST API (no browser)
cfplist          cfplist.com                  Playwright, headless
sciencedirect    ScienceDirect                Playwright, headed + DOM capture
sage             SAGE journals                nodriver (Cloudflare-stealth)
watchlist        INFORMS · OUP · Cambridge    nodriver, template-driven
─────────────────────────────────────────────────────────────────────────
master           →  normalize → dedupe (URL key) → relevance-tag → diff
                 →  CFP_master.xlsx   +   cfp_snapshot.json
```

Each publisher got the **minimum** machinery it required — escalating only when a simpler approach failed:

| Source | Tech encountered | Strategy |
|---|---|---|
| Taylor & Francis | Slow, stateful listing UI | Discovered the underlying **WP REST API** → plain pagination, no browser. Zero missing fields. |
| cfplist.com | AJAX pagination (URL params ignored) | Headless Playwright click-through. |
| ScienceDirect | React SPA, robots-disallowed, bot defense | Headed Playwright + network capture, DOM fallback. |
| SAGE (Atypon) | TLS fingerprinting, headless detection, **Cloudflare Turnstile** | `nodriver` + locale-agnostic challenge detection + multi-variant URL discovery. |
| INFORMS / OUP / Cambridge | Uniform per-journal URL patterns | One config: URL templates + journal codes + auto-discovery fallback. |

## Install

```bash
pip install requests curl_cffi nodriver playwright beautifulsoup4 pandas openpyxl
playwright install chromium
```

Requires Python 3.10+ and Google Chrome installed (for the `nodriver` stages).

## Usage

Run everything:

```bash
python cfp_radar_all.py
```

Run only some stages (comma-separated: `tandf`, `cfplist`, `sciencedirect`, `sage`, `watchlist`, `master`):

```bash
python cfp_radar_all.py --only tandf,master   # quick refresh: re-pull T&F, re-merge
python cfp_radar_all.py --skip sciencedirect  # skip the interactive stages
python cfp_radar_all.py --only master         # just re-merge existing CSVs
```

**Two stages open a browser window** and may need a moment of help:
- `sciencedirect` — press **Enter** in the console once the list has loaded.
- `sage` — if a Cloudflare checkbox appears, click it in the window (manual clicks work under `nodriver`).

For an unattended run, use `--skip sciencedirect,sage`.

The console reports per-source counts and the number of **new CFPs since the last run**. Results land in `CFP_master.xlsx`:

| Sheet | Contents |
|---|---|
| `New` | CFPs first seen in this run, relevance-sorted |
| `Active & relevant` | Open calls matching your target-journal / topic rules |
| `All` | Full deduplicated index with status (open / closed / unknown) |

> `cfp_snapshot.json` is the first-seen registry that powers the diff. Keep it next to the script — **don't delete it** between runs.

## Configuration

Everything you'd want to tune lives near the top of the relevant section in `cfp_radar_all.py`:

- **Relevance rules** — `TARGET_JOURNALS` and `TOPIC_KEYWORDS` regexes. Matching both → `★★`, either → `★`. Defaults target operations research, defense & security studies, and technology policy.
- **Journal watchlists** — `SAGE_WATCHLIST` and `PUB_WATCHLIST` lists (publisher, name, journal code). Add or remove journals here.

## Anti-bot engineering notes

The SAGE stage went through seven iterations. The failure chain is a compact tour of modern bot defense:

1. **`requests` + spoofed User-Agent → 403.** The platform fingerprints the TLS handshake; headers are irrelevant.
2. **Headless Chromium → 403.** The `HeadlessChrome` UA token and `navigator.webdriver` flag give it away.
3. **Headed Chromium → Cloudflare Turnstile loops forever**, even with a human clicking — Turnstile detects the CDP connection Playwright relies on.
4. **`nodriver` passes** — but challenge pages are served **in the visitor's locale**, so detection keyed on English strings silently accepted a Korean challenge page as real content. Fix: detect via the `<title>` tag across locales plus challenge-only variables — and *not* via `/cdn-cgi/challenge-platform`, which Cloudflare injects into legitimate pages too.
5. Final touches: ordinal date parsing ("30th September, 2026"), plural-aware link discovery ("Call**s** for Papers"), and scoping deadline extraction to each entry's nearest container so one section's date doesn't bleed onto every item.

## Responsible use

- Built for **personal research monitoring**: low volume, 1.5–3 s delays, no parallelism, public CFP pages only — nothing behind a paywall.
- Some sites disallow automated access in `robots.txt` or their terms. Review each site's terms and your local regulations before running the corresponding stage, and prefer official channels (e-mail alerts, RSS, publisher APIs) where they exist.
- Don't redistribute collected listings; deadlines change and stale mirrors mislead authors.

## Known limitations

- Some publishers' central listing pages are manually curated and incomplete; per-journal watchlists compensate.
- Top venues (e.g. *JCR*, *JPR*, *ISQ*, *International Affairs*) run **no open CFPs** by policy — special issues are assembled via guest-editor proposals, so a crawler correctly returns nothing there. Reach these through regular submission, or by proposing a themed issue yourself.
- Site markup and `cf_clearance` cookies change; expect occasional selector maintenance. Every stage dumps raw HTML to `debug_html/` for any page it can't parse, so fixes are diff-driven rather than guesswork.

## Roadmap

- Springer / Wiley journal-ID watchlists (templates already wired in)
- Scheduled runs + e-mail/Slack digest of the `New` sheet
- Per-paper matching: rank open calls against a manuscript abstract

## License

MIT — crawl responsibly.
