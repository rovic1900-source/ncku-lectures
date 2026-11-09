"""成大全校演講爬蟲：讀各系所公告 → 找出演講 → 判斷參加限制 → 產生 Google 日曆訂閱檔 (docs/lectures.ics)。"""
import hashlib
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from units import UNITS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = ROOT / "data" / "seen.json"
ICS_FILE = ROOT / "docs" / "lectures.ics"
HTML_FILE = ROOT / "docs" / "index.html"
TZ = timezone(timedelta(hours=8))
VERSION = 2
TODAY = datetime.now(TZ).date()

UA = "Mozilla/5.0 (compatible; ncku-lectures-bot; personal calendar)"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})

# ---------- 抓網頁 ----------

DEBUG = {}
FINAL_URL = {}


def fetch(url, _depth=0):
    for attempt in range(2):
        try:
            r = session.get(url, timeout=25)
            if r.status_code != 200:
                DEBUG.setdefault("http", {})[url] = r.status_code
                return None
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            text = r.text
            FINAL_URL[url] = r.url
            # 首頁只有一行 meta refresh 或 location.href 轉址的，跟過去
            m = re.search(r'http-equiv=["\']?refresh["\']?[^>]*url=([^"\'>\s]+)', text, re.I) or \
                re.search(r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', text)
            if m and len(text) < 3000 and _depth < 2:
                nxt = urljoin(r.url, html.unescape(m.group(1)))
                got = fetch(nxt, _depth + 1)
                if got:
                    FINAL_URL[url] = FINAL_URL.get(nxt, nxt)
                    return got
            return text
        except requests.RequestException as e:
            DEBUG.setdefault("error", {})[url] = type(e).__name__
            time.sleep(2)
    return None


LIST_WORDS = re.compile(r"消息|公告|演講|講座|活動|訊息|Seminar|News|Event|Talk|Colloquium|Lecture", re.I)
LIST_SKIP = re.compile(r"招生|徵才|求才|獎學金|榜單|法規|表單|下載|校友|English|英文", re.I)


def same_site(url, base):
    return urlparse(url).netloc.split(":")[0].endswith(urlparse(base).netloc.split(":")[0].replace("www.", ""))


def find_list_pages(home_url, soup):
    """首頁上找『最新消息 / 演講公告』之類的列表頁。成大 RPage 系統的列表頁網址是 /p/403-..."""
    lists = []
    for a in soup.find_all("a", href=True):
        href = urljoin(home_url, a["href"])
        text = a.get_text(" ", strip=True)
        if not same_site(href, home_url) or LIST_SKIP.search(text):
            continue
        if "/p/403-" in href or (LIST_WORDS.search(text) and len(text) <= 20):
            if href not in lists:
                lists.append(href)
    # RPage 首頁的公告區塊常用 JavaScript 載入，原始碼裡只留分類編號（Nbr=272 或公告網址的 ,r272）。
    # 用編號組出列表頁 /p/403-<網站編號>-<分類編號>-1.php
    raw = str(soup)
    site = re.search(r"/p/\d{3}-(\d+)-", raw)
    if site:
        base = "{0.scheme}://{0.netloc}".format(urlparse(home_url))
        nbrs = re.findall(r"Nbr=(\d+)", raw) + re.findall(r",r(\d+)\.php", raw)
        for n in dict.fromkeys(nbrs):
            u = f"{base}/p/403-{site.group(1)}-{n}-1.php?Lang=zh-tw"
            if not any(f"-{site.group(1)}-{n}-" in x for x in lists):
                lists.append(u)
    # 演講類列表優先
    lists.sort(key=lambda u: 0 if "/p/403-" in u else 1)
    return lists[:14]


def find_articles(page_url, soup):
    """列表頁上的公告連結。RPage 的公告內頁網址是 /p/406-..."""
    out = []
    rpage = any("/p/406-" in a["href"] for a in soup.find_all("a", href=True))
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("#")[0]
        text = a.get_text(" ", strip=True)
        if not same_site(href, page_url) or len(text) < 6:
            continue
        if rpage and "/p/406-" not in href:
            continue
        if href.lower().endswith((".pdf", ".doc", ".docx", ".jpg", ".png")):
            continue
        # 列表上同一列的日期（通常是發布日）
        ldate, node = None, a
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            row = node.get_text(" ", strip=True)
            if len(row) > len(text) + 120:
                break
            m = re.search(r"(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})", row.replace(text, ""))
            if m:
                try:
                    ldate = date(*map(int, m.groups()))
                except ValueError:
                    pass
                break
        out.append((href, text, ldate))
    return out


# ---------- 判斷是不是演講 ----------

TALK_TITLE = re.compile(
    r"演講|講座|講演|專講|演說|座談|論壇|工作坊|分享會|對談|講堂|"
    r"seminar|colloquium|lecture|talk|forum|workshop|symposium|webinar",
    re.I,
)
NOT_TALK = re.compile(r"徵聘|聘任|教師啟事|賀[!！]|榮獲|恭賀|得獎|餐敘|親師|徵稿|徵件|徵才|招生|錄取|名單|榜單|獎學金|補助|成果|花絮|回顧|紀實|側記|報導")

LABELS = {
    "speaker": {"講者", "主講人", "主講者", "演講者", "主講", "講師", "主持人暨講者", "speaker", "speakers", "lecturer", "presenter"},
    "topic": {"講題", "題目", "演講題目", "主題", "講座主題", "演講主題", "title", "topic"},
    "time": {"時間", "日期", "演講時間", "活動時間", "活動日期", "日期時間", "date", "time", "datetime", "when"},
    "place": {"地點", "演講地點", "活動地點", "場地", "地址", "venue", "location", "place", "where", "room"},
}


def labeled_fields(lines):
    found = {}
    for i, line in enumerate(lines):
        m = re.match(r"^[\W_]*([^:：]{1,14}?)\s*[:：]\s*(.*)$", line)
        if not m:
            continue
        label = re.sub(r"[\s\u3000*▍•]", "", m.group(1)).lower()
        value = m.group(2).strip()
        if not value and i + 1 < len(lines):
            value = lines[i + 1].strip()
        for key, names in LABELS.items():
            if label in names and key not in found and value:
                found[key] = value[:200]
    return found


# ---------- 日期時間 ----------

WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
           "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def find_weekday(after):
    """日期後面緊接的星期，例如 5/8(五)、11月9日（星期一）、3/12 Thu"""
    m = re.match(r"\s*[（(]?\s*(?:星期|週|周|禮拜)?\s*([一二三四五六日天])\s*[)）]?", after) or \
        re.match(r"\s*[（(,]?\s*(mon|tue|wed|thu|fri|sat|sun)", after, re.I)
    return WEEKDAY.get(m.group(1).lower()) if m else None


def term_year(ctx, month):
    """從「114-2」「115學年度第1學期」推年份"""
    m = re.search(r"(1[01]\d)\s*(?:學年度?)?\s*(?:-|第)\s*([12一二])\s*(?:學期)?", ctx or "")
    if not m:
        return None
    ay, term = int(m.group(1)), m.group(2) in ("1", "一")
    if term:
        return ay + 1911 if month >= 8 else ay + 1912
    return ay + 1912


def _year_guess(m, d, ref, wd=None, ctx="", pub=None):
    cands = []
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cands.append(date(y, m, d))
        except ValueError:
            pass
    if not cands:
        return None
    ty = term_year(ctx, m)
    if ty and any(c.year == ty for c in cands):
        return next(c for c in cands if c.year == ty)
    if wd is not None:
        hit = [c for c in cands if c.weekday() == wd]
        if len(hit) == 1:
            return hit[0]
        if hit:
            cands = hit
    if pub:
        # 活動通常在公告發布後才舉行：取發布日（前兩週）之後最早的那天
        after = [c for c in cands if c >= pub - timedelta(days=14)]
        if after:
            return min(after)
    # 不知道發布日：未來超過 4 個月的不太可能，偏向過去
    def cost(c):
        diff = (c - ref).days
        return diff * 2.5 if diff > 0 else -diff
    return min(cands, key=cost)


def parse_date(text, ref=TODAY, ctx="", pub=None):
    t = text.replace("／", "/").replace("．", ".")
    pats = [
        (r"(1[01]\d)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", lambda g: (int(g[0]) + 1911, int(g[1]), int(g[2]))),
        (r"(20\d\d)\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})", lambda g: (int(g[0]), int(g[1]), int(g[2]))),
        (r"(1[01]\d)[/.](\d{1,2})[/.](\d{1,2})(?!\d)", lambda g: (int(g[0]) + 1911, int(g[1]), int(g[2]))),
        (r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日", lambda g: (None, int(g[0]), int(g[1]))),
        (r"(?<![\d.:/])(\d{1,2})/(\d{1,2})(?![\d/])", lambda g: (None, int(g[0]), int(g[1]))),
    ]
    months = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
    for pat, conv in pats:
        for m in re.finditer(pat, t):
            y, mo, d = conv(m.groups())
            try:
                return date(y, mo, d) if y else _year_guess(mo, d, ref, find_weekday(t[m.end():m.end() + 8]), ctx, pub)
            except (ValueError, TypeError):
                continue
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d\d)?", t, re.I)
    if m:
        mo, d = months[m.group(1).lower()], int(m.group(2))
        try:
            return date(int(m.group(3)), mo, d) if m.group(3) else _year_guess(mo, d, ref, None, ctx, pub)
        except ValueError:
            pass
    return None


def _hm(h, mi, ampm):
    h, mi = int(h), int(mi or 0)
    if ampm and re.match(r"下午|晚上|晚間|pm|PM|p\.m\.", ampm) and h < 12:
        h += 12
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_times(text):
    t = text.replace("：", ":")
    pat = (r"(上午|早上|中午|下午|晚上|晚間)?\s*(\d{1,2}):(\d{2})\s*(am|pm|AM|PM|a\.m\.|p\.m\.)?"
           r"(?:\s*[-–—~～至到]\s*(上午|中午|下午|晚上)?\s*(\d{1,2}):(\d{2})\s*(am|pm|AM|PM|a\.m\.|p\.m\.)?)?")
    m = re.search(pat, t)
    if not m:
        m2 = re.search(r"(上午|下午|晚上)\s*(\d{1,2})\s*[點时時](半)?", t)
        if m2:
            s = _hm(m2.group(2), 30 if m2.group(3) else 0, m2.group(1))
            return s, None
        return None, None
    a1 = m.group(1) or m.group(4)
    start = _hm(m.group(2), m.group(3), a1)
    end = None
    if m.group(6):
        a2 = m.group(5) or m.group(8) or (a1 if a1 and not re.match(r"上午|早上", a1) else None)
        end = _hm(m.group(6), m.group(7), a2)
        if start and end and end <= start and end[0] < 12:
            end = (end[0] + 12, end[1])
    return start, end


# ---------- 參加限制 ----------

RESTRICT = re.compile(
    r"(僅|只|限)(限)?(本系|本所|本院|本學程|本系所|系上|所上|.{0,6}(學生|師生|研究生|大學部|碩士班|博士班|修課|會員|教職員))(參加|報名|參與)?|"
    r"(不|未)開放(校外|外系|一般|旁聽)|恕不開放|本系(所)?(師生|學生)(參加|參與)|修課同學|課程(演講|講座)|"
    r"only (for|open to)|limited to|restricted to|members only",
    re.I,
)
REGISTER = re.compile(r"(?<![無不免])需(事先)?報名|(?<![無不免])須(事先)?報名|請(先|事先)?(線上)?報名|報名(連結|網址|表單|系統|方式|截止)|"
                      r"registration (required|link|form)|please register|register (here|at|by)", re.I)
OPEN = re.compile(r"歡迎(全校|各界|校內外|有興趣|大家|踴躍|所有|師生|同學|蒞臨)|開放(全校|校外|外系|一般民眾|所有)|"
                  r"全校師生|自由(參加|入座|參與)|免報名|無需報名|不需報名|open to (all|the public|everyone)|all are welcome|everyone is welcome", re.I)


def snippet(m, text, n=18):
    s, e = max(0, m.start() - n), min(len(text), m.end() + n)
    return text[s:e].replace("\n", " ").strip()


def classify_access(text):
    tags, notes = [], []
    rm = RESTRICT.search(text)
    om = OPEN.search(text)
    if rm:
        tags.append("限定")
        notes.append("限定依據：…" + snippet(rm, text) + "…")
    elif om:
        tags.append("開放")
    gm = REGISTER.search(text)
    if not gm:
        # 任何「報名」字眼，只要前面不是 免/無需/不需/不用
        for m in re.finditer(r"報名", text):
            if not re.search(r"(免|無需|無須|不需|不須|不用)\s*$", text[max(0, m.start() - 3):m.start()]):
                gm = m
                break
    if gm:
        tags.append("需報名")
        notes.append("報名：…" + snippet(gm, text) + "…")
    if not tags:
        tags.append("未註明")
    return tags, notes


# ---------- 讀公告內頁 ----------

CONTENT_SEL = [".mpgdetail", ".module-detail", ".mcont", ".newsdetail", ".news-content", "article", "main", "#content", ".content"]


PUB_PATS = [
    r'(?:published_time|datePublished|pubdate)["\']?\s*(?:content=|:)\s*["\']?(20\d\d)-(\d\d)-(\d\d)',
    r"(?:發布|發佈|公告|張貼|刊登|上架|建立)(?:日期|時間|日)?\s*[:：]\s*(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})",
    r"(?:Post(?:ed)?|Publish(?:ed)?)\s*(?:Date|on)?\s*[:：]?\s*(20\d\d)[-/.](\d{1,2})[-/.](\d{1,2})",
]


def publish_date(page):
    for pat in PUB_PATS:
        m = re.search(pat, page, re.I)
        if m:
            try:
                return date(*map(int, m.groups()))
            except ValueError:
                pass
    return None


def parse_article(url, list_title, unit, list_date=None):
    page = fetch(url)
    if not page:
        return None
    pub = list_date or publish_date(page)
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "form"]):
        tag.decompose()
    node = None
    for sel in CONTENT_SEL:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 40:
            break
        node = None
    node = node or soup.body or soup
    for br in node.find_all(["br", "p", "li", "div", "tr", "h1", "h2", "h3", "h4"]):
        br.insert_after("\n")
    text = re.sub(r"[ \t\u00a0\u3000]+", " ", node.get_text())
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    body = "\n".join(lines)
    title = list_title
    h = soup.select_one(".hdline") or soup.find("h2") or soup.find("h1")
    if h and len(h.get_text(strip=True)) >= 6:
        title = h.get_text(" ", strip=True)

    f = labeled_fields(lines)
    title_hit = bool(TALK_TITLE.search(title)) and not NOT_TALK.search(title)
    body_hit = "speaker" in f and ("time" in f or "place" in f)
    if not (title_hit or body_hit):
        return {"lecture": False}

    when_src = " ".join(filter(None, [f.get("time"), title, body[:1500]]))
    ctx = title + " " + body[:400]
    d = (parse_date(f.get("time", ""), ctx=ctx, pub=pub) or parse_date(title, ctx=ctx, pub=pub)
         or parse_date(body[:1500], ctx=ctx, pub=pub))
    if not d:
        return {"lecture": False, "reason": "no date"}
    start, end = parse_times(f.get("time", "")) if f.get("time") else (None, None)
    if not start:
        start, end = parse_times(when_src[:600])

    tags, notes = classify_access(body)
    return {
        "lecture": True,
        "unit": unit,
        "url": url,
        "title": title,
        "topic": f.get("topic"),
        "speaker": f.get("speaker"),
        "place": f.get("place"),
        "time_raw": f.get("time"),
        "date": d.isoformat(),
        "start": list(start) if start else None,
        "end": list(end) if end else None,
        "tags": tags,
        "notes": notes,
        "found": TODAY.isoformat(),
        "pub": pub.isoformat() if pub else None,
        "v": VERSION,
    }


# ---------- 每個系所 ----------

def crawl_unit(unit, home, seen):
    page = fetch(home)
    if not page:
        print(f"  ! 打不開 {unit} {home}")
        return []
    home = FINAL_URL.get(home, home)
    soup = BeautifulSoup(page, "html.parser")
    candidates = find_articles(home, soup)
    list_pages = find_list_pages(home, soup)
    for lp in list_pages:
        lpage = fetch(lp)
        if lpage:
            candidates += find_articles(lp, BeautifulSoup(lpage, "html.parser"))
    uniq, results = {}, []
    for href, text, ldate in candidates:
        if href not in uniq or (ldate and not uniq[href][1]):
            uniq[href] = (text, ldate)
    # 新公告，加上用舊版程式判斷過、要重新判斷日期的演講
    redo = [h for h in uniq if seen.get(h, {}).get("lecture") and seen[h].get("v") != VERSION]
    new = [h for h in uniq if h not in seen][:80] + redo
    for href in new:
        text, ldate = uniq[href]
        # 非 RPage 網站的連結很雜，標題沒有演講字眼就跳過以免爬太多
        if "/p/406-" not in href and not TALK_TITLE.search(text):
            seen[href] = {"lecture": False}
            continue
        info = parse_article(href, text, unit, ldate)
        if info is not None:
            if seen.get(href, {}).get("found"):
                info["found"] = seen[href]["found"]
            seen[href] = info
        time.sleep(0.3)
    if not uniq:
        links = [urljoin(home, a["href"]) for a in soup.find_all("a", href=True)]
        DEBUG.setdefault("zero", {})[unit] = {
            "final_url": home, "html_len": len(page), "n_links": len(links),
            "list_pages": list_pages, "sample_links": links[:40],
            "scripts": [sc.get("src") for sc in soup.find_all("script", src=True)][:10],
            "head": page[:600],
        }
    print(f"  {unit}: {len(uniq)} 則公告，新讀 {len(new)} 則")
    return new


# ---------- 輸出 ----------

def ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 73:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def norm(s):
    return re.sub(r"[\W_]+", "", (s or "").lower())


def build_events(seen):
    events, alias = {}, {}
    horizon = TODAY - timedelta(days=60)
    for info in seen.values():
        if not info.get("lecture"):
            continue
        d = date.fromisoformat(info["date"])
        if d < horizon:
            continue
        keys = [("t", info["date"], norm(info.get("topic") or info["title"])[:40])]
        if info.get("speaker"):
            keys.append(("s", info["date"], norm(info["speaker"])[:12]))
        hit = next((k for k in keys if k in alias), None)
        if hit:
            e = alias[hit]
            if info["unit"] not in e["units"]:
                e["units"].append(info["unit"])
            # 保留資訊比較完整的那一筆
            if (not e.get("start") and info.get("start")) or len(e["title"]) < 10 <= len(info["title"]):
                units = e["units"]
                e.clear(); e.update(info, units=units)
            for k in keys:
                alias.setdefault(k, e)
            continue
        e = dict(info, units=[info["unit"]])
        events[id(e)] = e
        for k in keys:
            alias[k] = e
    return sorted(events.values(), key=lambda e: (e["date"], e["start"] or [0, 0]))


def tag_label(tags):
    if "限定" in tags:
        return "限定"
    if "開放" in tags:
        return "開放"
    return "未註明"


def write_ics(events):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ncku-lectures//TW", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", "X-WR-CALNAME:成大演講", "X-WR-TIMEZONE:Asia/Taipei", "REFRESH-INTERVAL;VALUE=DURATION:PT6H"]
    for e in events:
        d = date.fromisoformat(e["date"])
        name = e.get("topic") or e["title"]
        lab = tag_label(e["tags"])
        if "需報名" in e["tags"]:
            lab = "需報名" if lab == "未註明" else lab + "｜需報名"
        summary = f"【{lab}】{name}"
        if e.get("speaker"):
            summary += f"｜{e['speaker'][:30]}"
        desc = [f"主辦：{'、'.join(e['units'])}"]
        if e.get("speaker"):
            desc.append(f"講者：{e['speaker']}")
        if e.get("time_raw"):
            desc.append(f"時間：{e['time_raw']}")
        desc += e.get("notes", [])
        desc.append(f"公告：{e['url']}")
        uid = hashlib.md5(e["url"].encode()).hexdigest() + "@ncku-lectures"
        L += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}"]
        if e.get("start"):
            sdt = datetime(d.year, d.month, d.day, *e["start"], tzinfo=TZ)
            edt = datetime(d.year, d.month, d.day, *e["end"], tzinfo=TZ) if e.get("end") else sdt + timedelta(hours=2)
            if edt <= sdt:
                edt = sdt + timedelta(hours=2)
            fmt = lambda x: x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            L += [f"DTSTART:{fmt(sdt)}", f"DTEND:{fmt(edt)}"]
        else:
            L += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        L += [f"SUMMARY:{ics_escape(summary)}", f"DESCRIPTION:{ics_escape(chr(10).join(desc))}", f"URL:{e['url']}"]
        if e.get("place"):
            L.append(f"LOCATION:{ics_escape(e['place'])}")
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    ICS_FILE.write_text("\r\n".join(fold(x) for x in L) + "\r\n", encoding="utf-8")


def gcal_link(e):
    d = date.fromisoformat(e["date"])
    if e.get("start"):
        sdt = datetime(d.year, d.month, d.day, *e["start"], tzinfo=TZ)
        edt = datetime(d.year, d.month, d.day, *e["end"], tzinfo=TZ) if e.get("end") else sdt + timedelta(hours=2)
        if edt <= sdt:
            edt = sdt + timedelta(hours=2)
        f = lambda x: x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dates = f"{f(sdt)}/{f(edt)}"
    else:
        dates = f"{d:%Y%m%d}/{d + timedelta(days=1):%Y%m%d}"
    details = [f"主辦：{'、'.join(e['units'])}"]
    if e.get("speaker"):
        details.append(f"講者：{e['speaker']}")
    details += e.get("notes", [])
    details.append(e["url"])
    q = {"action": "TEMPLATE", "text": e.get("topic") or e["title"], "dates": dates,
         "details": "\n".join(details), "location": e.get("place") or "", "ctz": "Asia/Taipei"}
    return "https://calendar.google.com/calendar/render?" + urlencode(q)


def write_html(events):
    upcoming = [e for e in events if e["date"] >= TODAY.isoformat()]
    new_since = (TODAY - timedelta(days=2)).isoformat()
    data = [{
        "id": hashlib.md5(e["url"].encode()).hexdigest()[:12],
        "date": e["date"],
        "time": f"{e['start'][0]:02d}:{e['start'][1]:02d}" if e.get("start") else "",
        "title": e.get("topic") or e["title"],
        "units": "、".join(e["units"]),
        "speaker": (e.get("speaker") or "")[:60],
        "place": (e.get("place") or "")[:60],
        "tags": e["tags"],
        "new": e.get("found", "") >= new_since,
        "url": e["url"],
        "gcal": gcal_link(e),
    } for e in upcoming]
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = PAGE_TEMPLATE.replace("__DATA__", payload).replace(
        "__UPDATED__", f"{datetime.now(TZ):%m/%d %H:%M}").replace("__NUNITS__", str(len(UNITS)))
    HTML_FILE.write_text(page, encoding="utf-8")


PAGE_TEMPLATE = r"""<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>成大演講</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--card:#f5f5f7;--acc:#b5121b;--accfg:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#f2f2f7;--mut:#a1a1a6;--line:#2c2c2e;--card:#1c1c1e;--acc:#ff6961;--accfg:#111}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif}
main{max-width:720px;margin:0 auto;padding:16px 16px 60px}
h1{font-size:22px;margin:0}.sub{color:var(--mut);font-size:13px;margin-bottom:10px}
#q{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);font-size:16px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:10px 0;scrollbar-width:none}
.chips button{flex:0 0 auto;border:1px solid var(--line);background:none;color:var(--fg);border-radius:99px;padding:5px 12px;font-size:14px}
.chips button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.day{font-weight:700;font-size:14px;color:var(--mut);margin:18px 0 6px}
.item{background:var(--card);border-radius:12px;padding:12px;margin-bottom:8px}
.item.added{outline:2px solid #34c759}
.top{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}
.time{font-weight:600;font-size:14px}
.title{display:block;font-weight:600;color:var(--fg);text-decoration:none;margin:2px 0}
.meta{color:var(--mut);font-size:13px}
.tag{font-size:12px;padding:1px 7px;border-radius:5px;white-space:nowrap}
.t-開放{background:#e3f6e8;color:#1b6b34}.t-限定{background:#fde8e8;color:#9b1c1c}
.t-需報名{background:#fff4d6;color:#7a5a00}.t-未註明{background:#e9e9ee;color:#555}.t-新{background:var(--acc);color:var(--accfg)}
.acts{display:flex;gap:8px;margin-top:10px}
.acts a,.acts button{flex:1;text-align:center;padding:8px;border-radius:9px;font-size:14px;text-decoration:none;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.acts a.cal{background:var(--acc);color:var(--accfg);border-color:var(--acc);font-weight:600}
.empty{color:var(--mut);text-align:center;padding:40px 0}
.foot{margin-top:20px;font-size:13px;color:var(--mut);text-align:center}
.foot button{background:none;border:none;color:var(--acc);font-size:13px;text-decoration:underline}
</style></head><body><main>
<h1>成大演講</h1>
<div class="sub">全校 __NUNITS__ 個單位 · 更新於 __UPDATED__ · 每天 6:00、14:00 自動更新</div>
<input id="q" type="search" placeholder="搜尋講題、講者、系所…">
<div class="chips" id="chips"></div>
<div id="list"></div>
<div class="foot" id="foot"></div>
</main>
<script>
const DATA = __DATA__;
const FILTERS = [["全部",null],["新公布","新"],["開放","開放"],["未註明","未註明"],["需報名","需報名"],["限定","限定"],["已加入","__added"]];
const W = ["日","一","二","三","四","五","六"];
function load(k){try{return new Set(JSON.parse(localStorage.getItem(k)||"[]"))}catch(e){return new Set()}}
function save(k,s){try{localStorage.setItem(k,JSON.stringify([...s]))}catch(e){}}
let hidden=load("hidden"), added=load("added"), filter=null, showHidden=false;
const chips=document.getElementById("chips");
FILTERS.forEach(([lab,f],i)=>{const b=document.createElement("button");b.textContent=lab;if(!i)b.className="on";
 b.onclick=()=>{filter=f;[...chips.children].forEach(x=>x.classList.toggle("on",x===b));render()};chips.appendChild(b)});
document.getElementById("q").oninput=render;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function render(){
 const q=document.getElementById("q").value.trim().toLowerCase();
 let html="",last="",n=0;
 for(const e of DATA){
  const tags=e.new?["新",...e.tags]:e.tags;
  if(!showHidden&&hidden.has(e.id))continue;
  if(filter==="__added"){if(!added.has(e.id))continue}
  else if(filter&&!tags.includes(filter))continue;
  if(q&&!(e.title+e.units+e.speaker+e.place).toLowerCase().includes(q))continue;
  if(e.date!==last){const d=new Date(e.date+"T00:00:00+08:00");
   html+=`<div class="day">${d.getMonth()+1}/${d.getDate()}（${W[d.getDay()]}）</div>`;last=e.date}
  n++;
  const isH=hidden.has(e.id), isA=added.has(e.id);
  html+=`<div class="item${isA?" added":""}" data-id="${e.id}"><div class="top"><span class="time">${e.time||"時間見公告"}</span>
   ${tags.map(t=>`<span class="tag t-${t}">${t}</span>`).join("")}</div>
   <a class="title" href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>
   <div class="meta">${esc(e.units)}${e.speaker?" · "+esc(e.speaker):""}${e.place?" · "+esc(e.place):""}</div>
   <div class="acts"><a class="cal" href="${esc(e.gcal)}" target="_blank" rel="noopener" data-act="add">${isA?"✓ 已加入":"加到日曆"}</a>
   <button data-act="hide">${isH?"取消隱藏":"隱藏"}</button></div></div>`;
 }
 document.getElementById("list").innerHTML=html||`<div class="empty">沒有符合的演講</div>`;
 const hc=DATA.filter(e=>hidden.has(e.id)).length;
 document.getElementById("foot").innerHTML=hc?`已隱藏 ${hc} 場 · <button id="sh">${showHidden?"收起":"顯示"}</button>`:"";
 const sh=document.getElementById("sh"); if(sh)sh.onclick=()=>{showHidden=!showHidden;render()};
}
document.getElementById("list").addEventListener("click",ev=>{
 const t=ev.target.closest("[data-act]"); if(!t)return;
 const id=t.closest(".item").dataset.id;
 if(t.dataset.act==="add"){added.add(id);save("added",added);setTimeout(render,300)}
 else{hidden.has(id)?hidden.delete(id):hidden.add(id);save("hidden",hidden);render()}
});
render();
</script></body></html>"""


def main():
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    # 每個系所用一條執行緒，系所內部依序抓，避免對同一網站太密集
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda u: crawl_unit(u[0], u[1], seen), UNITS))
    # 清掉太舊的紀錄以免檔案越來越大（非演講的保留 URL 以免重抓）
    # 舊版判斷、這次沒重讀到的演講（已不在列表上），日期可能猜錯，不放進日曆
    seen = {k: (v if not v.get("lecture") or v.get("v") == VERSION else {"lecture": False, "reason": "stale"})
            for k, v in seen.items()}
    cutoff = (TODAY - timedelta(days=120)).isoformat()
    seen = {k: v for k, v in seen.items() if not v.get("lecture") or v["date"] >= cutoff}
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    (ROOT / "data" / "debug.json").write_text(json.dumps(DEBUG, ensure_ascii=False, indent=1), encoding="utf-8")
    events = build_events(seen)
    ICS_FILE.parent.mkdir(exist_ok=True)
    write_ics(events)
    write_html(events)
    print(f"完成：{len(events)} 場演講寫入日曆")


if __name__ == "__main__":
    main()
