# FindCalls - 
Find CFPs from Publishers

**An automated pipeline that tracks special-issue calls for papers (CFPs) across five academic publishing ecosystems, merges them into a single master sheet, and flags newly posted calls on every run.**

Academic CFPs are scattered across publisher portals with wildly different tech stacks — static HTML, AJAX pagination, WordPress REST APIs, and aggressively bot-protected platforms. CFP Radar treats each source with the lightest technique that actually works, normalizes everything into one schema, and answers the only question that matters between runs: *"What's new?"*

Built by a defense operations-research analyst to stop manually checking ten journal pages a week. As of the initial index: **4,230 unique CFPs** across **5 sources**.

---

## Architecture

```
┌─────────────┐  ┌──────────────┐  ┌─────────────┐  ┌───────────┐  ┌──────────────────┐
│  cfplist.com │  │ ScienceDirect│  │ Taylor &     │  │   SAGE    │  │ Journal watchlist │
│  (Playwright)│  │ (Playwright/ │  │ Francis      │  │ (nodriver │  │ INFORMS · OUP ·   │
│              │  │  DOM capture)│  │ (WP REST API)│  │  + CF     │  │ Cambridge         │
│              │  │              │  │              │  │  bypass)  │  │ (nodriver)        │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────┬─────┘  └────────┬─────────┘
       │ CSV             │ CSV             │ CSV            │ CSV             │ CSV
       └────────────┬────┴─────────────────┴────────────────┴─────────────────┘
                    ▼
             cfp_master.py  ──  normalize → dedupe (URL key) → relevance-tag
                    │
                    ├──► CFP_master.xlsx   [ New | Active & relevant | All ]
                    └──► cfp_snapshot.json (first-seen registry → diff on next run)
```

## Source-by-source strategy

Each source got the **minimum** machinery it required — escalating only when a simpler approach failed:

| Source | Stack encountered | Technique used |
|---|---|---|
| cfplist.com | AJAX pagination (URL params ignored) | Playwright click-through, 23 pages |
| ScienceDirect | React SPA, robots-disallowed, strong bot defense | Headed Playwright + network-response capture, DOM fallback (≈97% of listed CFPs recovered) |
| Taylor & Francis | WordPress; listing UI is slow and stateful | **Discovered the underlying WP REST API** via network capture → pure `requests` pagination. No browser. 917 records, zero missing fields |
| SAGE (Atypon) | TLS fingerprinting → 403 for `requests`; headless detection; Cloudflare Turnstile that defeats Playwright (CDP detection); **localized challenge pages** | `nodriver` (CDP-stealth browser) + locale-agnostic challenge detection + multi-variant CFP-URL discovery (`/cfp`, `/call-for-papers`, home-page link scan with plural-aware matching) |
| INFORMS / OUP / Cambridge | Uniform per-journal CFP URL patterns (Atypon / Silverchair) | One config-driven watchlist crawler: URL templates + journal codes + auto-discovery fallback |

## Repository layout

```
cfplist_crawler.py            # Source 1 — cfplist.com (Playwright)
sciencedirect_cfp_crawler.py  # Source 2 — ScienceDirect (Playwright, dual-strategy)
tandf_cfp_api.py              # Source 3 — Taylor & Francis (REST API, recommended)
tandf_cfp_crawler.py          #   └ legacy browser version (kept for reference)
sage_cfp_crawler_v7.py        # Source 4 — SAGE (nodriver; v1–v6 document the escalation)
watchlist_cfp_crawler.py      # Source 5 — INFORMS / OUP / Cambridge journal watchlist
cfp_master.py                 # Merge + dedupe + relevance tags + diff
cfp_snapshot.json             # First-seen registry (do not delete)
CFP_master.xlsx               # Output: [New] [Active & relevant] [All]
```

## Requirements

- Python 3.10+
- Google Chrome installed (for the `nodriver`-based crawlers)

```bash
pip install requests curl_cffi nodriver playwright beautifulsoup4 pandas openpyxl
playwright install chromium
```

## Usage

**1. Refresh the sources** (each writes its own CSV to the working directory):

```bash
python tandf_cfp_api.py             # fastest — pure API, ~1 min
python sciencedirect_cfp_crawler.py # browser window opens; press Enter when the list loads
python cfplist_crawler.py
python sage_cfp_crawler_v7.py       # browser window opens; click the Cloudflare
                                    # checkbox if prompted — manual clicks work
                                    # under nodriver
python watchlist_cfp_crawler.py
```

You don't need all five every time; `cfp_master.py` picks up whichever CSVs are present.

**2. Rebuild the master and see what's new:**

```bash
python cfp_master.py
```

Console output reports per-source counts and the number of **new CFPs since the last run**; `CFP_master.xlsx` opens with three sheets:

| Sheet | Contents |
|---|---|
| `New` | CFPs first seen in this run, relevance-sorted |
| `Active & relevant` | Open calls matching the target-journal / topic rules |
| `All` | Full deduplicated index with status (open / closed / unknown) |

**3. Tune relevance to your field.** Edit two regexes at the top of `cfp_master.py` (`TARGET_JOURNALS`, `TOPIC_KEYWORDS`). Matching both earns `★★`, either one `★`. The defaults are tuned for operations research, defense & security studies, and technology policy.

## Anti-bot engineering notes

The SAGE crawler went through seven versions; the failure chain is a compact case study in modern bot defense, so it's documented here:

1. **`requests` + spoofed User-Agent → 403.** Atypon fingerprints the TLS handshake; headers are irrelevant.
2. **Headless Chromium (Playwright) → 403.** `HeadlessChrome` UA token and `navigator.webdriver` give it away.
3. **Headed Chromium → Cloudflare Turnstile loops forever**, even with a human clicking the checkbox: Turnstile detects the CDP (DevTools protocol) connection Playwright relies on.
4. **`nodriver` passes** — but a subtle bug remained: challenge pages are **served in the visitor's locale**. Detection keyed on English strings ("Just a moment…") silently accepted Korean challenge pages ("잠시만 기다리십시오…") as content. Fix: detect via the `<title>` tag across locales plus challenge-only script variables — and *not* via `/cdn-cgi/challenge-platform`, which Cloudflare injects into legitimate pages too.
5. Final touches that mattered: date parsing for ordinal formats ("30th September, 2026"), plural-aware link discovery ("Call**s** for Papers"), and scoping deadline extraction to each entry's nearest container so one section's date doesn't propagate to every item.

## Responsible use

- Built for **personal research monitoring**: low volume, 1.5–3 s delays between requests, no parallelism, no paywalled content — only public CFP announcement pages.
- Some target sites disallow automated access in `robots.txt` or their terms of service. Review each site's terms and your local regulations before running the corresponding crawler; consider official channels (e-mail alerts, RSS, publisher APIs) where they exist.
- Do not redistribute collected listings; deadlines change and stale mirrors mislead authors.

## Known limitations

- Central listing pages at some publishers are manually curated and incomplete; per-journal watchlists compensate.
- Top-tier venues (e.g., *JCR*, *JPR*, *ISQ*, *International Affairs*) run **no open CFPs** by policy — special issues are assembled via guest-editor proposals. A crawler correctly returns nothing there; monitor these via regular submission or by proposing a themed issue yourself.
- `cf_clearance` cookies and site markup change; expect occasional selector maintenance. Every crawler dumps raw HTML for any page it fails to parse, so fixes are diff-driven rather than guesswork.

## Roadmap

- Springer / Wiley journal-ID watchlists (templates already wired)
- Scheduled runs + e-mail/Slack digest of the `New` sheet
- Per-paper matching: rank open calls against a manuscript abstract

## Author

**Duncan Kim** — operations research & technology policy researcher.
*Python · web-data engineering · MILP/LP optimization · defense analytics.*

License: MIT (crawl responsibly).
