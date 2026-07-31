# -*- coding: utf-8 -*-
"""
FindCalls — 올인원 실행 파일
=============================
5개 소스 크롤링 → 마스터 통합 → 신규(diff) 감지를 한 파일로 수행한다.
각 스테이지는 기존 개별 크롤러(검증 완료 버전)의 로직을 그대로 이식했다.

실행:
  py findcalls.py                    # 전체 스테이지 순차 실행
  py findcalls.py --sciencedirect    # 이 소스만 재크롤링 (+ master 자동 재실행)
  py findcalls.py --sciencedirect --sage   # 여러 소스 재크롤링
  py findcalls.py --only tandf,master
  py findcalls.py --skip sciencedirect,cfplist
  py findcalls.py --master           # 기존 CSV만 재통합
  py findcalls.py --delete           # 이전 산출물 삭제(스냅샷 보존)
  py findcalls.py --delete-all       # 스냅샷까지 완전 초기화

스테이지 (실행 순서):
  tandf         T&F WordPress REST API   (브라우저 불필요, ~1분)
  cfplist       cfplist.com              (Playwright, 자동)
  sciencedirect ScienceDirect            (Playwright 창 + Enter 1회)
  sage          SAGE 중앙+저널           (nodriver 창, Cloudflare 수동클릭 가능)
  watchlist     INFORMS/OUP/Cambridge    (nodriver, sage와 브라우저 공유)
  aclweb        ACL Portal(NLP 학회/워크숍) (requests, 정적 테이블)
  master        통합 + 관심태그 + diff   (CFP_master.xlsx / cfp_snapshot.json)

플래그 우선순위: 개별 소스 플래그(--sciencedirect 등) > --only > 기본(전체).
  - 개별 소스 플래그로 재크롤링하면 그 소스의 CSV만 갱신되고, 이어서
    master가 자동 실행되어 CFP_master.xlsx에 반영된다(--no-master로 끔).
  - 예: 방금 실행에서 sciencedirect가 로드 타이밍을 놓쳤다면
    → py findcalls.py --sciencedirect  로 그 소스만 다시 받으면 된다.

설치:
  pip install requests curl_cffi nodriver playwright beautifulsoup4 pandas openpyxl
  playwright install chromium
비고:
  - 실패한 스테이지는 경고 후 건너뛰며, master는 존재하는 CSV만 사용한다.
  - 개별 소스를 재크롤링해도 나머지 소스의 CSV는 그대로 유지되므로,
    master는 항상 '가장 최근에 받은 모든 소스'를 통합한다.
  - cfp_snapshot.json은 지금까지 관측한 CFP 기록(diff 기준)이므로 삭제 금지.
    삭제하면 다음 실행에서 전체가 '신규'로 표시된다.
"""
import argparse
import asyncio
import json
import re
import sys
import time
import traceback
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════
# 공통 유틸
# ═══════════════════════════════════════════════════════════════════════
TODAY = pd.Timestamp(date.today())
DEBUG_DIR = Path("debug_html")

TITLE_MARKERS = ("just a moment", "잠시만 기다리", "checking your browser",
                 "attention required")
BODY_MARKERS = ("_cf_chl_opt", "cf_chl_rt_tk")


def looks_blocked(html):
    if not html or len(html) < 600:
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", html[:4000], re.I | re.S)
    title = (m.group(1) if m else "").strip().lower()
    if any(t in title for t in TITLE_MARKERS):
        return True
    return any(b in html[:8000] for b in BODY_MARKERS)


DL_PAT = re.compile(
    r"(?:submission|abstract|proposal|manuscript)s?[^.\n]{0,40}?"
    r"(?:deadline|due|by|close)s?\s*:?\s*([^\n<]{3,120})", re.I)
DATE_PAT = re.compile(
    r"(\d{1,2}(?:st|nd|rd|th)?\s*(?:of\s+)?[A-Z][a-z]+,?\s*\d{4}"
    r"|[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})")


def parse_deadline(text):
    m = DL_PAT.search(text)
    raw = m.group(1).strip() if m else ""
    dates = DATE_PAT.findall(raw or text)
    dt = None
    if dates:
        cand = re.sub(r"(\d)([A-Z])", r"\1 \2", dates[-1])
        cand = re.sub(r"(\d{1,2})(st|nd|rd|th)\b", r"\1", cand)
        cand = re.sub(r"\bof\s+", "", cand).replace(",", "")
        for fmt in ("%d %B %Y", "%B %d %Y"):
            try:
                dt = pd.to_datetime(cand, format=fmt)
                break
            except Exception:
                continue
        if dt is None:
            dt = pd.to_datetime(cand, errors="coerce")
    return raw, dt


def dump_debug(name, html):
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"{name}.html").write_text(html or "", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# 공유 nodriver fetcher (SAGE + watchlist 스테이지가 브라우저 1개 공유)
# ═══════════════════════════════════════════════════════════════════════
class NoDriverFetcher:
    _inst = None

    @classmethod
    def shared(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    @classmethod
    def close_shared(cls):
        if cls._inst is not None:
            cls._inst.close()
            cls._inst = None

    def __init__(self):
        import nodriver as uc
        self._uc = uc
        self._loop = asyncio.new_event_loop()
        self._browser = self._loop.run_until_complete(self._start())

    async def _start(self):
        print("[fetcher] nodriver 브라우저 기동 (창이 뜹니다)")
        return await self._uc.start(headless=False)

    async def _wait_challenge(self, tab):
        for i in range(90):
            html = await tab.get_content()
            if not looks_blocked(html):
                await asyncio.sleep(1.0)
                return True
            if i == 3:
                print("  챌린지 감지 — 자동 통과 대기 중 "
                      "(체크박스가 보이면 창에서 직접 클릭)")
            await asyncio.sleep(2)
        return False

    async def _fetch(self, url, ok404):
        tab = await self._browser.get(url)
        await asyncio.sleep(1.2)
        ok = await self._wait_challenge(tab)
        html = await tab.get_content()
        if not ok or looks_blocked(html):
            print(f"  차단 지속: {url[:75]}")
            return None
        if ok404 and re.search(
                r"(page not found|error[- ]?page|cannot be found|"
                r"page you requested)", html[:5000], re.I):
            return None
        return html

    def get(self, url, ok404=False):
        return self._loop.run_until_complete(self._fetch(url, ok404))

    def close(self):
        try:
            self._browser.stop()
        except Exception:
            pass
        try:
            self._loop.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 1: Taylor & Francis (WP REST API — tandf_cfp_api.py 이식)
# ═══════════════════════════════════════════════════════════════════════
def stage_tandf():
    import requests
    BASE = "https://think.taylorandfrancis.com/wp-json/wp/v2"
    HEAD = {"User-Agent": "Mozilla/5.0 (research use)"}
    PER = 100
    s = requests.Session()
    s.headers.update(HEAD)

    def gj(url, params=None, retries=3):
        for i in range(retries):
            try:
                r = s.get(url, params=params, timeout=60)
                if r.status_code == 400:
                    return None, r.headers
                r.raise_for_status()
                return r.json(), r.headers
            except Exception as e:
                print(f"  재시도 {i+1}/{retries}: {e}")
                time.sleep(3 * (i + 1))
        return None, {}

    def subject_map():
        mp, page = {}, 1
        while True:
            data, _ = gj(f"{BASE}/special_issues_tax_subject_areas",
                         {"per_page": PER, "page": page, "_fields": "id,name"})
            if not data:
                break
            for t in data:
                mp[t["id"]] = t["name"]
            if len(data) < PER:
                break
            page += 1
            time.sleep(1)
        print(f"  분야 태그 {len(mp)}개 로드")
        return mp

    def first(meta, *keys):
        for k in keys:
            v = meta.get(k)
            if isinstance(v, list) and v and str(v[0]).strip():
                return str(v[0]).strip()
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def strip_html(x):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x or "")).strip()

    def fetch_type(ptype, smap):
        p = "_special_issues" if ptype == "special_issues" \
            else "_article_collections"
        rows, page, total = [], 1, None
        while True:
            data, hdrs = gj(
                f"{BASE}/{ptype}",
                {"per_page": PER, "page": page,
                 "_fields": f"id,link,title,{ptype},"
                            f"special_issues_tax_subject_areas"})
            if data is None:
                break
            if total is None:
                total = hdrs.get("X-WP-TotalPages", "?")
                print(f"  [{ptype}] 총 {hdrs.get('X-WP-Total','?')}건 / "
                      f"{total}페이지")
            for it in data:
                meta = it.get(ptype, {}) or {}
                subj = it.get("special_issues_tax_subject_areas", []) or []
                editors = "; ".join(
                    e for e in (first(meta, f"{p}_editor{i}_name")
                                for i in range(1, 9)) if e)
                rows.append({
                    "type": ptype,
                    "title": strip_html(
                        it.get("title", {}).get("rendered", "")),
                    "journal": first(meta, f"{p}_journal_title"),
                    "abstract_deadline": first(meta, f"{p}_deadline2"),
                    "manuscript_deadline": first(meta, f"{p}_deadline"),
                    "subject_areas": "; ".join(
                        smap.get(i, str(i)) for i in subj),
                    "editors": editors,
                    "open_access": first(meta, "_open_access") == "1",
                    "journal_link": first(meta, f"{p}_journal_link"),
                    "submit_link": first(meta, f"{p}_submissions_submit",
                                         f"{p}_submissions_link"),
                    "cfp_page": it.get("link", ""),
                })
            print(f"  페이지 {page}/{total} — 누적 {len(rows)}건")
            if len(data) < PER:
                break
            page += 1
            time.sleep(1)
        return rows

    smap = subject_map()
    rows = fetch_type("special_issues", smap)
    time.sleep(1)
    rows += fetch_type("article_collections", smap)
    df = pd.DataFrame(rows).drop_duplicates(subset="cfp_page")
    df["manuscript_deadline_dt"] = pd.to_datetime(
        df["manuscript_deadline"], format="%d %B %Y", errors="coerce")
    df["abstract_deadline_dt"] = pd.to_datetime(
        df["abstract_deadline"], format="%d %B %Y", errors="coerce")
    df = df.sort_values("manuscript_deadline_dt")
    df.to_csv("tandf_cfps_v2.csv", index=False, encoding="utf-8-sig")
    print(f"  저장: tandf_cfps_v2.csv ({len(df)}건)")


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 2: cfplist.com (Playwright sync — cfplist_crawler.py 이식)
# ═══════════════════════════════════════════════════════════════════════
def stage_cfplist():
    from playwright.sync_api import sync_playwright
    BASE = "https://cfplist.com/"
    rows, seen = [], set()
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(BASE, wait_until="networkidle")
        try:
            page.click("text=Essential Only", timeout=3000)
        except Exception:
            pass
        body = page.inner_text("body")
        m = re.search(r"Page\s+1\s+of\s+(\d+)", body)
        total = min(int(m.group(1)) if m else 30, 30)
        print(f"  총 {total}페이지 감지")
        for pg in range(1, total + 1):
            if pg > 1:
                try:
                    page.click(f"a:has-text('{pg}')", timeout=5000)
                except Exception:
                    try:
                        page.click("a:has-text('Next')", timeout=5000)
                    except Exception:
                        break
                page.wait_for_load_state("networkidle")
                time.sleep(1.5)
            items = page.eval_on_selector_all(
                "a[href*='/CFP/']",
                """els => els.map(a => {
                    if (a.closest('h4') === null) return null;
                    const card = a.closest('div');
                    return {href: a.href, title: a.innerText.trim(),
                            cardText: card ? card.innerText : ''};
                }).filter(x => x)""")
            for it in items:
                mid = re.search(r"/CFP/(\d+)", it["href"])
                if not mid:
                    continue
                cid = int(mid.group(1))
                if cid in seen:
                    continue
                seen.add(cid)
                txt = it["cardText"]
                ev = re.search(r"EVENT\s+([A-Za-z]{3}\s+\d{1,2})", txt)
                ab = re.search(r"ABSTRACT\s+([A-Za-z]{3}\s+\d{1,2})", txt)
                dl = re.search(r"DAYS\s+(\d+)\s+LEFT", txt)
                rows.append({
                    "cfp_id": cid, "title": it["title"],
                    "event_date": ev.group(1) if ev else "",
                    "abstract_deadline": ab.group(1) if ab else "",
                    "days_left": int(dl.group(1)) if dl else None,
                    "url": it["href"],
                })
            print(f"  페이지 {pg}/{total} — 누적 {len(rows)}건")
        b.close()
    pd.DataFrame(rows).sort_values("cfp_id", ascending=False).to_csv(
        "cfplist_all.csv", index=False, encoding="utf-8-sig")
    print(f"  저장: cfplist_all.csv ({len(rows)}건)")


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 3: ScienceDirect (Playwright headed — DOM 전략 이식)
# ═══════════════════════════════════════════════════════════════════════
def stage_sciencedirect():
    from playwright.sync_api import sync_playwright
    URL = "https://www.sciencedirect.com/browse/calls-for-papers"
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)
        page = b.new_context(viewport={"width": 1400, "height": 900})\
                .new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        input("  브라우저에서 목록이 정상 표시되면 Enter "
              "(차단 페이지가 뜨면 통과 후 Enter): ")
        for pg in range(1, 101):
            time.sleep(3.0)
            try:
                items = page.eval_on_selector_all(
                    "a[href*='call' i], a[href*='special-issue' i]",
                    """els => els.map(a => ({
                        href: a.href, text: a.innerText.trim(),
                        cardText: a.closest('li,article,div') ?
                                  a.closest('li,article,div').innerText : ''
                    }))""")
                rows += [it for it in items if it["text"]]
            except Exception as e:
                print(f"  DOM 파싱 경고: {e}")
            nxt = page.locator(
                "a[aria-label*='next' i], button[aria-label*='next' i], "
                "a:has-text('Next'), button:has-text('Next')").first
            try:
                if nxt.is_visible(timeout=3000):
                    if nxt.get_attribute("aria-disabled") == "true":
                        print("  마지막 페이지 도달")
                        break
                    nxt.click()
                    page.wait_for_load_state("networkidle", timeout=30000)
                    print(f"  페이지 {pg + 1}로 이동")
                else:
                    break
            except Exception:
                break
        b.close()
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["href"])
    if df.empty:
        # 0건 수집(로드 타이밍 놓침/차단 등): 기존 정상 CSV를 덮어쓰지 않는다.
        # 빈 파일을 남기면 이후 master가 EmptyDataError로 실패하기 때문.
        print("  [경고] ScienceDirect 0건 수집 — 기존 CSV를 보존하고 "
              "저장을 건너뜁니다. (--sciencedirect 로 재시도하세요)")
        return
    df.to_csv("sciencedirect_cfps.csv", index=False, encoding="utf-8-sig")
    print(f"  저장: sciencedirect_cfps.csv ({len(df)}건)")


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 4: SAGE (nodriver — sage_cfp_crawler_v7 이식)
#   ※ jcr/jpr/afs/sdi/cmp/ire/sth/ssc는 CFP 페이지가 없음이 확인되어 제외
# ═══════════════════════════════════════════════════════════════════════
SAGE_BASE = "https://journals.sagepub.com"
SAGE_WATCHLIST = [
    ("Journal of Defense Modeling and Simulation", "dms"),
    ("SIMULATION (Trans. of SCS)", "sim"),
    ("International Journal of Micro Air Vehicles", "mav"),
    ("Public Policy and Administration", "ppa"),
]
CFP_LINK_TEXT = re.compile(r"calls?\s*[-–]?\s*for\s*[-–]?\s*papers?", re.I)
CFP_LINK_HREF = re.compile(
    r"/(cfp|calls?-?for-?papers?|callforpapers?)(/|$|\?)", re.I)


def _sage_central(f):
    html = f.get(f"{SAGE_BASE}/special-issue-calls-for-papers")
    if not html:
        print("  중앙 페이지 접근 실패")
        return []
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for h3 in soup.find_all("h3"):
        discipline = h3.get_text(strip=True)
        block = []
        for sib in h3.find_next_siblings():
            if sib.name == "h3":
                break
            block.append(sib)
        journal = ""
        for el in block:
            for a in el.find_all("a", href=True):
                href, text = a["href"], a.get_text(" ", strip=True)
                if not text:
                    continue
                if "/home/" in href:
                    journal = text
                elif re.search(r"/page/|/doi/|\.pdf", href):
                    raw, dt = "", None
                    node = a
                    for _ in range(4):
                        node = node.parent
                        if node is None or node is el.parent:
                            break
                        ctx = node.get_text("\n", strip=True)
                        if len(ctx) > 420:
                            break
                        raw, dt = parse_deadline(ctx)
                        if raw:
                            break
                    rows.append({
                        "source": "central", "discipline": discipline,
                        "journal": journal, "si_title": text,
                        "deadline_raw": raw, "deadline_dt": dt,
                        "url": urljoin(SAGE_BASE, href),
                    })
    dump_debug("central", html)
    return rows


def _sage_find_pages(f, code):
    pages = []
    for path in (f"/page/{code}/cfp",
                 f"/page/{code}/call-for-papers",
                 f"/page/{code}/callforpapers",
                 f"/page/{code}/call-for-papers/special-collections"):
        html = f.get(SAGE_BASE + path, ok404=True)
        if html and "call" in html.lower():
            pages.append((SAGE_BASE + path, html))
            break
    if pages:
        return pages
    home = f.get(f"{SAGE_BASE}/home/{code}", ok404=True)
    if not home:
        return []
    soup = BeautifulSoup(home, "html.parser")
    cands, seen = [], set()
    for a in soup.find_all("a", href=True):
        if CFP_LINK_TEXT.search(a.get_text(" ", strip=True)) \
           or CFP_LINK_HREF.search(a["href"]):
            u = urljoin(SAGE_BASE, a["href"]).split("?")[0].split("#")[0]
            if u not in seen and "sagepub.com" in u:
                seen.add(u)
                cands.append(u)
    for u in cands[:4]:
        html = f.get(u, ok404=True)
        if html:
            pages.append((u, html))
    return pages


def stage_sage():
    f = NoDriverFetcher.shared()
    rows = _sage_central(f)
    print(f"  중앙 페이지: {len(rows)}건")
    for name, code in SAGE_WATCHLIST:
        time.sleep(2.5)
        pages = _sage_find_pages(f, code)
        if not pages:
            print(f"  [탐지 실패] {name} (code={code})")
            continue
        found = 0
        for url, html in pages:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if len(t) < 15 or len(t) > 200:
                    continue
                if not (a["href"].startswith("/page") or
                        a["href"].startswith("/doi") or
                        "sagepub.com/page" in a["href"] or
                        "sagepub.com/doi" in a["href"] or
                        ".pdf" in a["href"]):
                    continue
                ctx_el = a.find_parent(["p", "li", "div"]) or a
                ctx = ctx_el.get_text("\n", strip=True)
                if not re.search(r"deadline|submission", ctx, re.I):
                    continue
                raw, dt = parse_deadline(ctx)
                rows.append({
                    "source": "watchlist", "discipline": "",
                    "journal": name, "si_title": t,
                    "deadline_raw": raw, "deadline_dt": dt,
                    "url": urljoin(SAGE_BASE, a["href"]),
                })
                found += 1
            if found == 0:
                dump_debug(f"journal_{code}", html)
        print(f"  {name}: {found}건")
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["journal", "si_title"])
    df.to_csv("sage_cfps.csv", index=False, encoding="utf-8-sig")
    print(f"  저장: sage_cfps.csv ({len(df)}건)")


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 5: INFORMS / OUP / Cambridge 워치리스트
#   ※ OUP isq/isr/fpa/jogss/cybersecurity/ia/policyandsociety/rev 는
#     공개 CFP 제도가 없음이 확인되어 제외 (상시투고·게스트에디터 제안형)
# ═══════════════════════════════════════════════════════════════════════
PUBLISHERS = {
    "INFORMS": {
        "base": "https://pubsonline.informs.org",
        "cfp_templates": ["/page/{code}/calls-for-papers",
                          "/page/{code}/call-for-papers"],
        "home_template": "/journal/{code}",
    },
    "OUP": {
        "base": "https://academic.oup.com",
        "cfp_templates": ["/{code}/pages/calls-for-papers",
                          "/{code}/pages/call-for-papers",
                          "/{code}/pages/call_for_papers"],
        "home_template": "/{code}",
    },
    "Cambridge": {
        "base": "https://www.cambridge.org",
        "cfp_templates":
            ["/core/journals/{code}/announcements/call-for-papers"],
        "home_template": "/core/journals/{code}",
    },
}
PUB_WATCHLIST = [
    ("INFORMS", "Operations Research", "opre"),
    ("INFORMS", "Management Science", "mnsc"),
    ("INFORMS", "Mathematics of Operations Research", "moor"),
    ("INFORMS", "Transportation Science", "trsc"),
    ("INFORMS", "Decision Analysis", "deca"),
    ("INFORMS", "INFORMS Journal on Computing", "ijoc"),
    ("INFORMS", "INFORMS Journal on Optimization", "ijoo"),
    ("INFORMS", "INFORMS Journal on Data Science", "ijds"),
    ("INFORMS", "Manufacturing & Service Operations Management", "msom"),
    ("INFORMS", "Information Systems Research", "isre"),
    ("INFORMS", "INFORMS Journal on Applied Analytics", "inte"),
    ("OUP", "Science and Public Policy", "spp"),
    ("OUP", "Industrial and Corporate Change", "icc"),
    ("Cambridge", "Data & Policy", "data-and-policy"),
]
WL_LINK_HREF = re.compile(
    r"/(cfp|calls?-?for-?papers?|call_for_papers|collections|announcements)"
    r"(/|$|\?)", re.I)


def _wl_parse_page(html, publisher, journal, url):
    soup = BeautifulSoup(html, "html.parser")
    region = (soup.find("main") or soup.find(id="main-content")
              or soup.find("article") or soup.body or soup)
    rows = []
    for h in region.find_all(["h2", "h3", "h4"]):
        title = h.get_text(" ", strip=True)
        if not (12 <= len(title) <= 220):
            continue
        if re.search(r"(cookie|sign in|navigat|footer|related|also from|"
                     r"about|keep up|information for|submission guideline)",
                     title, re.I):
            continue
        parts = []
        for sib in h.find_next_siblings():
            if sib.name in ("h2", "h3", "h4"):
                break
            parts.append(sib.get_text("\n", strip=True))
        block = "\n".join(x for x in parts if x)[:3000]
        if not re.search(r"deadline|submission|submit|due", block, re.I):
            continue
        raw, dt = parse_deadline(block)
        rows.append({
            "publisher": publisher, "journal": journal, "si_title": title,
            "deadline_raw": raw, "deadline_dt": dt, "url": url,
        })
    return rows


def _wl_find_pages(f, pub, code):
    conf = PUBLISHERS[pub]
    base = conf["base"]
    for tmpl in conf["cfp_templates"]:
        url = base + tmpl.format(code=code)
        html = f.get(url, ok404=True)
        if html and re.search(r"call|collection|special", html, re.I):
            return [(url, html)]
    home = f.get(base + conf["home_template"].format(code=code), ok404=True)
    if not home:
        return []
    soup = BeautifulSoup(home, "html.parser")
    host = urlparse(base).netloc
    cands, seen = [], set()
    for a in soup.find_all("a", href=True):
        if CFP_LINK_TEXT.search(a.get_text(" ", strip=True)) \
           or WL_LINK_HREF.search(a["href"]):
            u = urljoin(base, a["href"]).split("?")[0].split("#")[0]
            if host in u and u not in seen:
                seen.add(u)
                cands.append(u)
    pages = []
    for u in cands[:4]:
        html = f.get(u, ok404=True)
        if html:
            pages.append((u, html))
    return pages


def stage_watchlist():
    f = NoDriverFetcher.shared()
    rows = []
    for pub, name, code in PUB_WATCHLIST:
        time.sleep(2.5)
        pages = _wl_find_pages(f, pub, code)
        if not pages:
            print(f"  [탐지 실패] {pub} | {name} (code={code})")
            continue
        found = 0
        for url, html in pages:
            got = _wl_parse_page(html, pub, name, url)
            rows.extend(got)
            found += len(got)
            if not got:
                dump_debug(f"{pub}_{code}", html)
        print(f"  {pub} | {name}: {found}건")
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["journal", "si_title"])
    df.to_csv("watchlist_cfps.csv", index=False, encoding="utf-8-sig")
    print(f"  저장: watchlist_cfps.csv ({len(df)}건)")


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 6: ACL Portal 이벤트 (NLP 학회/워크숍 CFP)
#   aclweb.org/portal/events 는 정렬 가능한 정적 HTML 테이블 —
#   제목·장소·도시·국가·게시일·제출마감·행사일 컬럼. 봇 차단 없음 → requests.
#   ACL·EMNLP·NAACL·EACL·CoNLL 메인 + 워크숍/shared task CFP가 모두 게시됨.
#   ※ 테이블에 마감일이 이미 구조화되어 있어 OpenReview API(제출물 중심,
#     마감일 미제공)보다 이 소스가 목적에 부합.
# ═══════════════════════════════════════════════════════════════════════
ACL_EVENTS = "https://www.aclweb.org/portal/events"
ACL_MAX_PAGES = 6          # 안전 상한 (현재 3페이지)


def stage_aclweb():
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (research use)"}
    rows, seen = [], set()
    for pg in range(0, ACL_MAX_PAGES):
        url = ACL_EVENTS if pg == 0 else f"{ACL_EVENTS}?page={pg}"
        try:
            r = requests.get(url, headers=headers, timeout=45)
            r.raise_for_status()
        except Exception as e:
            print(f"  페이지 {pg} 요청 실패({type(e).__name__}) — 중단")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            break
        body = table.find("tbody") or table
        page_rows = 0
        for tr in body.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 7:
                continue
            link = cells[0].find("a", href=True)
            title = cells[0].get_text(" ", strip=True)
            href = urljoin(ACL_EVENTS, link["href"]) if link else ""
            if not title or (href and href in seen):
                continue
            if href:
                seen.add(href)
            location = cells[1].get_text(" ", strip=True)
            country = cells[3].get_text(" ", strip=True)
            deadline_raw = cells[5].get_text(" ", strip=True)
            event_dates = cells[6].get_text(" ", strip=True)
            # 마감일 파싱: "31 Mar 2026" 형식
            dt = pd.to_datetime(deadline_raw, format="%d %b %Y",
                                errors="coerce")
            rows.append({
                "title": title,
                "venue": (location or country).strip(),
                "deadline_raw": deadline_raw,
                "deadline_dt": dt,
                "event_dates": event_dates,
                "url": href,
            })
            page_rows += 1
        print(f"  페이지 {pg + 1}: {page_rows}건 (누적 {len(rows)})")
        # 다음 페이지 링크가 없으면 종료
        if not soup.find("a", href=re.compile(rf"[?&]page={pg + 1}\b")):
            break
        time.sleep(1.5)
    df = pd.DataFrame(rows)
    if df.empty:
        print("  [경고] ACL Portal 0건 — 기존 CSV 보존, 저장 건너뜀.")
        return
    df = df.drop_duplicates(subset="url")
    df.to_csv("aclweb_cfps.csv", index=False, encoding="utf-8-sig")
    n_act = (df["deadline_dt"] >= pd.Timestamp.today().normalize()).sum()
    print(f"  저장: aclweb_cfps.csv ({len(df)}건, 마감 유효 {n_act}건)")


# ═══════════════════════════════════════════════════════════════════════
# 스테이지 7: 마스터 통합 + diff (cfp_master.py 이식)
# ═══════════════════════════════════════════════════════════════════════
SNAPSHOT = Path("cfp_snapshot.json")
TARGET_JOURNALS = re.compile(
    r"(European Journal of Operational Research|\bOmega\b|"
    r"Computers & Operations Research|Operations Research|Management Science|"
    r"Mathematics of Operations Research|Transportation Science|"
    r"Decision Analysis|INFORMS Journal|M&SOM|Manufacturing & Service|"
    r"Information Systems Research|Engineering Optimization|"
    r"International Journal of Production Research|IISE|"
    r"Energy Policy|Research Policy|Technological Forecasting|Technovation|"
    r"Defen[cs]e|Security|Strategic Studies|Military|Simulation|"
    r"Reliability Engineering|Decision Support|Expert Systems|"
    r"Transportation Research|Technology in Society|"
    r"Energy Research & Social Science|Applied Energy|Applied Economics|"
    r"Science and Public Policy|Industrial and Corporate Change|"
    r"Public Policy|Data & Policy|Korea)", re.I)
TOPIC_KEYWORDS = re.compile(
    r"(defen[cs]e|military|security|war\b|UAV|drone|unmanned|kill.?chain|"
    r"semiconductor|chip\b|supply chain|dual.use|geopolit|deterren|nuclear|"
    r"AI governance|artificial intelligence|generative AI|LLM|"
    r"large language|machine learning|reinforcement learning|"
    r"game.theor|decision.mak|optimi[sz]|operations research|"
    r"vehicle routing|routing|scheduling|quantum|"
    r"energy (security|transition|polic)|critical infrastructure|"
    r"Korea|technology transfer|innovation polic|rare earth|"
    r"critical mineral|"
    # NLP/LLM 연구 라인 (ACL Portal 등 학회 CFP 대응)
    r"natural language|\bNLP\b|computational lingu|language model|"
    r"retrieval.augmented|\bRAG\b|question answer|multilingual|"
    r"low.resource|evaluation|benchmark|agentic|multimodal)", re.I)
MASTER_COLS = ["출처", "저널/주최", "제목", "초록마감", "원고마감", "마감원문",
               "상태", "관심", "최초관측", "URL"]


def _norm_key(url, journal, title):
    if isinstance(url, str) and url.startswith("http"):
        return url.split("?")[0].split("#")[0].rstrip("/").lower()
    return (str(journal).strip().lower() + "||" +
            str(title).strip().lower())[:300]


def _to_dt(x):
    return pd.to_datetime(x, errors="coerce")


def _safe_read_csv(path):
    """CSV를 안전하게 읽는다.
    파일이 없거나(None), 0바이트/헤더만 있는 빈 파일이거나, 파싱이
    깨진 경우 None을 반환해 호출부가 해당 소스를 건너뛰게 한다.
    (예: 크롤러가 0건 수집 후 빈 파일을 남긴 경우 → master가 죽지 않음)"""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(p, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        print(f"  [경고] {path} 가 비어 있어 건너뜁니다 "
              f"(해당 소스는 재크롤링 필요).")
        return None
    except Exception as e:
        print(f"  [경고] {path} 읽기 실패({type(e).__name__}) — 건너뜁니다.")
        return None
    if df.empty:
        print(f"  [경고] {path} 에 데이터 행이 없어 건너뜁니다.")
        return None
    return df


def _load_cfplist():
    df = _safe_read_csv("cfplist_all.csv")
    if df is None:
        return []
    return [{"출처": "cfplist", "저널/주최": "",
             "제목": r.get("title", ""),
             "초록마감": _to_dt(r.get("abstract_deadline")),
             "원고마감": pd.NaT,
             "마감원문": str(r.get("abstract_deadline") or ""),
             "URL": r.get("url", "")} for _, r in df.iterrows()]


def _load_sd():
    df = _safe_read_csv("sciencedirect_cfps.csv")
    if df is None or "href" not in df.columns:
        return []
    df = df[df["href"].astype(str).str.contains("/special-issue/", na=False)]
    rows = []
    for _, r in df.iterrows():
        t = str(r.get("cardText") or "")
        lines = [l.strip().replace("\xa0", " ")
                 for l in t.split("\n") if l.strip()]
        title = lines[0] if lines else ""
        jline = next((l for l in lines
                      if "Impact Factor" in l or "CiteScore" in l), "")
        journal = jline.split("\u2022")[0].strip() if jline else ""
        m_dl = re.search(r"Submission deadline:\s*(.+)", t)
        raw = m_dl.group(1).strip() if m_dl else ""
        if not journal:
            di = next((i for i, l in enumerate(lines)
                       if l.startswith("Submission deadline")), None)
            if di and di >= 1 and not lines[di - 1].lower()\
                    .startswith("guest editor") and lines[di - 1] != title:
                journal = lines[di - 1]
        rows.append({"출처": "ScienceDirect", "저널/주최": journal,
                     "제목": title, "초록마감": pd.NaT,
                     "원고마감": pd.to_datetime(raw, format="%d %B %Y",
                                                errors="coerce"),
                     "마감원문": raw, "URL": r.get("href", "")})
    return rows


def _load_tandf():
    df = _safe_read_csv("tandf_cfps_v2.csv")
    if df is None:
        return []
    return [{"출처": "T&F", "저널/주최": r.get("journal", ""),
             "제목": r.get("title", ""),
             "초록마감": _to_dt(r.get("abstract_deadline_dt")
                                or r.get("abstract_deadline")),
             "원고마감": _to_dt(r.get("manuscript_deadline_dt")
                                or r.get("manuscript_deadline")),
             "마감원문": str(r.get("manuscript_deadline") or ""),
             "URL": r.get("cfp_page", "")} for _, r in df.iterrows()]


def _load_generic(path, label):
    df = _safe_read_csv(path)
    if df is None:
        return []
    rows = []
    for _, r in df.iterrows():
        pub = r.get("publisher")
        rows.append({"출처": pub if isinstance(pub, str) and pub else label,
                     "저널/주최": r.get("journal", ""),
                     "제목": r.get("si_title", ""),
                     "초록마감": pd.NaT,
                     "원고마감": _to_dt(r.get("deadline_dt")),
                     "마감원문": str(r.get("deadline_raw") or ""),
                     "URL": r.get("url", "")})
    return rows


def _load_aclweb():
    df = _safe_read_csv("aclweb_cfps.csv")
    if df is None:
        return []
    return [{"출처": "ACL", "저널/주최": str(r.get("venue") or ""),
             "제목": r.get("title", ""),
             "초록마감": pd.NaT,
             "원고마감": _to_dt(r.get("deadline_dt")),
             "마감원문": str(r.get("deadline_raw") or ""),
             "URL": r.get("url", "")} for _, r in df.iterrows()]


def stage_master():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    rows = (_load_cfplist() + _load_sd() + _load_tandf()
            + _load_generic("sage_cfps.csv", "SAGE")
            + _load_generic("watchlist_cfps.csv", "Watchlist")
            + _load_aclweb())
    if not rows:
        print("  입력 CSV가 없습니다 — 크롤러 스테이지를 먼저 실행하세요.")
        return
    df = pd.DataFrame(rows)
    df["제목"] = df["제목"].astype(str).str.strip()
    df = df[df["제목"].str.len() >= 8]
    df["키"] = [_norm_key(u, j, t) for u, j, t in
                zip(df["URL"], df["저널/주최"], df["제목"])]
    df = df.drop_duplicates(subset="키")
    df["상태"] = ["미상" if pd.isna(m) and pd.isna(a)
                  else ("진행중" if (m if pd.notna(m) else a) >= TODAY
                        else "마감")
                  for m, a in zip(df["원고마감"], df["초록마감"])]
    df["관심"] = [("★★" if (TARGET_JOURNALS.search(str(j) or "")
                            and TOPIC_KEYWORDS.search(str(t) or ""))
                   else "★" if (TARGET_JOURNALS.search(str(j) or "")
                                or TOPIC_KEYWORDS.search(str(t) or ""))
                   else "")
                  for j, t in zip(df["저널/주최"], df["제목"])]

    seen = json.loads(SNAPSHOT.read_text(encoding="utf-8")) \
        if SNAPSHOT.exists() else {}
    today_s = str(date.today())
    first_seen, is_new = [], []
    for k in df["키"]:
        if k in seen:
            first_seen.append(seen[k])
            is_new.append(False)
        else:
            seen[k] = today_s
            first_seen.append(today_s)
            is_new.append(True)
    df["최초관측"] = first_seen
    df["신규"] = is_new
    SNAPSHOT.write_text(json.dumps(seen, ensure_ascii=False),
                        encoding="utf-8")

    wb = Workbook()
    hf = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    bf = Font(name="Arial", size=10)
    fill = PatternFill("solid", fgColor="1F4E79")

    def sheet(ws, d):
        for c, h in enumerate(MASTER_COLS, 1):
            cell = ws.cell(1, c, h)
            cell.font = hf
            cell.fill = fill
        for r, (_, row) in enumerate(d.iterrows(), 2):
            vals = [row["출처"], row["저널/주최"], row["제목"],
                    "" if pd.isna(row["초록마감"])
                    else row["초록마감"].date().isoformat(),
                    "" if pd.isna(row["원고마감"])
                    else row["원고마감"].date().isoformat(),
                    row["마감원문"], row["상태"], row["관심"],
                    row["최초관측"], row["URL"]]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(r, c, v)
                cell.font = bf
                cell.alignment = Alignment(vertical="top",
                                           wrap_text=(c == 3))
        for i, w in enumerate([13, 30, 58, 11, 11, 22, 7, 6, 11, 42], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if len(d):
            ws.auto_filter.ref = f"A1:J{len(d) + 1}"

    new_df = df[df["신규"]].sort_values(["관심", "원고마감"],
                                        ascending=[False, True])
    ws1 = wb.active
    ws1.title = f"신규({len(new_df)})"
    sheet(ws1, new_df)
    hot = df[(df["상태"] == "진행중") & (df["관심"] != "")]\
        .sort_values(["관심", "원고마감"], ascending=[False, True])
    ws2 = wb.create_sheet(f"진행중_관심({len(hot)})")
    sheet(ws2, hot)
    ws3 = wb.create_sheet(f"전체({len(df)})")
    sheet(ws3, df.sort_values(["상태", "원고마감"]))
    wb.save("CFP_master.xlsx")

    print("  소스별 건수:")
    for k, v in df["출처"].value_counts().items():
        print(f"    {k}: {v}")
    print(f"  전체 {len(df)}건 | 진행중 {(df['상태'] == '진행중').sum()}건 | "
          f"진행중·관심 {((df['상태'] == '진행중') & (df['관심'] != '')).sum()}건")
    print(f"  이번 실행 신규: {df['신규'].sum()}건 → CFP_master.xlsx [신규] 시트")


# ═══════════════════════════════════════════════════════════════════════
# 정리(cleanup) — 이전 실행 산출물 삭제
# ═══════════════════════════════════════════════════════════════════════
# 크롤러/통합이 생성하는 산출물 목록 (스냅샷은 별도 취급)
OUTPUT_FILES = [
    "cfplist_all.csv",
    "sciencedirect_cfps.csv",
    "tandf_cfps_v2.csv",
    "sage_cfps.csv",
    "watchlist_cfps.csv",
    "aclweb_cfps.csv",
    "CFP_master.xlsx",
]
OUTPUT_DIRS = ["debug_html"]
SNAPSHOT_FILE = "cfp_snapshot.json"


def stage_delete(include_snapshot=False, assume_yes=False):
    """이전 실행 산출물을 삭제한다.
    기본: CSV들 + CFP_master.xlsx + debug_html/ (스냅샷 보존).
    include_snapshot=True: cfp_snapshot.json 까지 삭제 → diff 이력 초기화."""
    import shutil

    targets = [Path(f) for f in OUTPUT_FILES if Path(f).exists()]
    dir_targets = [Path(d) for d in OUTPUT_DIRS if Path(d).is_dir()]
    snap = Path(SNAPSHOT_FILE)
    snap_hit = include_snapshot and snap.exists()

    if not targets and not dir_targets and not snap_hit:
        print("  삭제할 산출물이 없습니다.")
        return

    print("  삭제 대상:")
    for p in targets:
        print(f"    - {p.name}")
    for d in dir_targets:
        n = sum(1 for _ in d.rglob('*') if _.is_file())
        print(f"    - {d.name}/  (파일 {n}개)")
    if snap_hit:
        print(f"    - {snap.name}  ⚠ diff 이력 초기화 "
              f"(다음 실행에서 전체가 '신규'로 표시됨)")

    if not assume_yes:
        prompt = ("  정말 삭제할까요? "
                  + ("[스냅샷 포함] " if snap_hit else "")
                  + "되돌릴 수 없습니다 (y/N): ")
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("  취소되었습니다.")
            return

    for p in targets:
        try:
            p.unlink()
        except Exception as e:
            print(f"    [실패] {p.name}: {e}")
    for d in dir_targets:
        try:
            shutil.rmtree(d)
        except Exception as e:
            print(f"    [실패] {d.name}/: {e}")
    if snap_hit:
        try:
            snap.unlink()
        except Exception as e:
            print(f"    [실패] {snap.name}: {e}")

    print("  삭제 완료."
          + ("" if include_snapshot
             else f" (스냅샷 {SNAPSHOT_FILE} 은 보존 — "
                  f"완전 초기화는 --delete-all)"))


# ═══════════════════════════════════════════════════════════════════════
# 오케스트레이션
# ═══════════════════════════════════════════════════════════════════════
STAGES = [
    ("tandf", stage_tandf, "Taylor & Francis (REST API)"),
    ("cfplist", stage_cfplist, "cfplist.com (Playwright)"),
    ("sciencedirect", stage_sciencedirect, "ScienceDirect (Playwright 창)"),
    ("sage", stage_sage, "SAGE (nodriver 창)"),
    ("watchlist", stage_watchlist, "INFORMS/OUP/Cambridge (nodriver)"),
    ("aclweb", stage_aclweb, "ACL Portal (NLP 학회/워크숍, requests)"),
    ("master", stage_master, "마스터 통합 + diff"),
]


def main():
    crawl_keys = [k for k, _, _ in STAGES if k != "master"]

    ap = argparse.ArgumentParser(
        description="FindCalls 올인원 실행",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "예시:\n"
            "  py findcalls.py                    # 전체 스테이지 순차 실행\n"
            "  py findcalls.py --sciencedirect    # 이 소스만 재크롤링 후 master 재실행\n"
            "  py findcalls.py --sciencedirect --sage   # 여러 소스 재크롤링\n"
            "  py findcalls.py --only tandf,master\n"
            "  py findcalls.py --skip sciencedirect,cfplist\n"
            "  py findcalls.py --master           # 기존 CSV만 재통합\n"
            "\n개별 소스 플래그(--sciencedirect 등)로 특정 소스만 다시 받으면\n"
            "master를 자동으로 다시 실행해 결과(CFP_master.xlsx)에 반영합니다.\n"
            "--no-master 로 그 자동 재통합을 끌 수 있습니다.\n"
            "\n정리:\n"
            "  py findcalls.py --delete       # 산출물(CSV·xlsx·debug_html) 삭제,\n"
            "                                 #   스냅샷은 보존(diff 이력 유지)\n"
            "  py findcalls.py --delete-all   # 위 + cfp_snapshot.json 까지 초기화\n"
            "  py findcalls.py --delete --yes # 확인 프롬프트 없이 삭제"))

    # 개별 스테이지 플래그: --tandf --cfplist --sciencedirect --sage
    #                        --watchlist --master
    for key, _, desc in STAGES:
        ap.add_argument(f"--{key}", action="store_true",
                        help=f"'{key}' 스테이지 실행 ({desc})")
    ap.add_argument("--only", help="쉼표구분: 이 스테이지들만 실행")
    ap.add_argument("--skip", help="쉼표구분: 이 스테이지들은 건너뜀")
    ap.add_argument("--no-master", action="store_true",
                    help="개별 소스 재크롤링 후 master 자동 재실행을 하지 않음")
    ap.add_argument("--delete", action="store_true",
                    help="이전 산출물(CSV·CFP_master.xlsx·debug_html) 삭제 "
                         "(스냅샷 보존)")
    ap.add_argument("--delete-all", action="store_true",
                    help="이전 산출물 + cfp_snapshot.json 까지 완전 초기화")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="삭제 확인 프롬프트를 건너뜀")
    args = ap.parse_args()

    # 정리 옵션은 크롤링/통합보다 우선 처리하고 종료
    if args.delete or args.delete_all:
        print("\n=== [delete] 이전 실행 산출물 정리 ===")
        stage_delete(include_snapshot=args.delete_all, assume_yes=args.yes)
        return

    valid = {k for k, _, _ in STAGES}

    # 실행할 스테이지 결정 (우선순위: 개별 플래그 > --only > 기본 전체)
    selected_flags = {k for k in valid if getattr(args, k.replace("-", "_"))}

    if selected_flags:
        run = set(selected_flags)
        # 소스 스테이지를 하나라도 개별 지정했고, master를 명시하지 않았으며,
        # --no-master도 아니면 → master를 자동으로 뒤에 붙인다.
        recrawled_source = bool(selected_flags & set(crawl_keys))
        if recrawled_source and "master" not in run and not args.no_master:
            run.add("master")
            print("[info] 재크롤링 후 master를 자동 재실행합니다 "
                  "(끄려면 --no-master).")
    elif args.only:
        run = set(args.only.split(","))
    else:
        run = set(valid)  # 기본: 전체

    skip = set(args.skip.split(",")) if args.skip else set()

    for s in run | skip:
        if s not in valid:
            sys.exit(f"알 수 없는 스테이지: {s} (가능: {', '.join(valid)})")

    ok, failed = [], []
    try:
        for key, fn, desc in STAGES:      # STAGES 순서 유지 → master가 항상 마지막
            if key not in run or key in skip:
                continue
            print(f"\n=== [{key}] {desc} ===")
            try:
                fn()
                ok.append(key)
            except KeyboardInterrupt:
                raise
            except Exception:
                failed.append(key)
                print(f"  [스테이지 실패] {key} — 건너뛰고 계속합니다.")
                traceback.print_exc(limit=2)
    finally:
        NoDriverFetcher.close_shared()

    print(f"\n완료: {', '.join(ok) if ok else '없음'}"
          + (f" | 실패: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
