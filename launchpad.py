#!/usr/bin/env python3
"""Launchpad: local internship tracker. Standard library only. Serves http://127.0.0.1:8765"""
import hashlib, html, json, os, re, sqlite3, sys, threading, time, secrets
import urllib.request, urllib.error, urllib.robotparser as rp
import socket, ipaddress
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
from datetime import datetime, timezone, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import dateintel as di

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("LAUNCHPAD_DB", os.path.join(HERE, "launchpad.sqlite3")); FIRMS = os.path.join(HERE, "firms.json")
UA = "LaunchpadPersonalTracker/1.0 (personal use, low rate)"
LOCK = threading.Lock(); SYNC = {"running": False, "last": None}; PAGE_DELAY = 0.5
now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")

def get(url, limit=2_000_000, data=None):
    if urlparse(url).scheme != "https": raise ValueError("https only")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json,text/html", **({"Content-Type": "application/json"} if data else {})})
    with urllib.request.urlopen(req, timeout=15) as r:
        b = r.read(limit + 1)
        if len(b) > limit: raise ValueError("response too large")
        return r.status, r.geturl(), b.decode(r.headers.get_content_charset() or "utf-8", "replace")

_rob = {}
def allowed(url):
    h = urlparse(url).netloc
    if h not in _rob:
        p = rp.RobotFileParser()
        try: s, _, t = get(f"https://{h}/robots.txt", 200000); p.parse(t.splitlines() if s == 200 else [])
        except Exception: p.parse([])
        _rob[h] = p
    return _rob[h].can_fetch(UA, url)

def strip(h): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(h or ""))).strip()
def a_greenhouse(s):  # updated_at is NOT a posting date, so it is deliberately ignored
    d = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=true", 8_000_000)[2])
    return [dict(role=j["title"], url=j["absolute_url"], loc=(j.get("location") or {}).get("name", ""), opened=None, desc=strip(j.get("content"))) for j in d["jobs"]]
def a_lever(s):
    d = json.loads(get(f"https://api.lever.co/v0/postings/{s}?mode=json", 8_000_000)[2])
    ts = lambda j: datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc).date().isoformat() if j.get("createdAt") else None
    return [dict(role=j["text"], url=j["hostedUrl"], loc=(j.get("categories") or {}).get("location", ""), opened=ts(j), desc=j.get("descriptionPlain", "")) for j in d]
def a_ashby(s):
    d = json.loads(get(f"https://api.ashbyhq.com/posting-api/job-board/{s}", 8_000_000)[2])
    return [dict(role=j["title"], url=j["jobUrl"], loc=j.get("location", ""), opened=(j.get("publishedAt") or "")[:10] or None, desc=j.get("descriptionPlain", "")) for j in d["jobs"]]
def clean(u):  # drop tracking parameters such as utm_source=linkedin so the same job is never stored twice
    p = urlsplit(u); q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_") and k.lower() not in ("gclid", "fbclid")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))
def jobposting(h):
    p = di._Text(); p.feed(h)
    for b in p.ld:
        try: data = json.loads(b)
        except ValueError: continue
        for jp in di._jobposting(data): return jp
def a_sitemap(host):
    """For employer sites with their own job pages (Phenom, Oracle and others). slug = host, e.g. careers.uk.leonardo.com.
    Job URLs come from the site's sitemap, short-term titles are picked from the URL, and a job is kept only if its page has JobPosting structured data."""
    base = f"https://{host}"; maps = []
    try: maps = [l.split(":", 1)[1].strip() for l in get(base + "/robots.txt", 200000)[2].splitlines() if l.lower().startswith("sitemap:")]
    except Exception: pass
    queue, urls, seen = (maps or [base + "/sitemap.xml"])[:6], [], set()
    while queue and len(urls) < 5000:
        m = queue.pop(0)
        if m in seen: continue
        seen.add(m)
        try: x = get(m, 8_000_000)[2]
        except Exception: continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x):
            loc = html.unescape(loc)
            if not loc.startswith(base + "/"): continue
            if loc.endswith(".xml") and len(queue) < 12: queue.append(loc)
            elif "/job/" in loc: urls.append(loc)
    out = []
    for u in dict.fromkeys(urls):
        title = u.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        if not INC.search(title) or EXC.search(title): continue
        try:
            if not allowed(u): continue
            jp = jobposting(get(u)[2]); time.sleep(PAGE_DELAY)
        except Exception: continue
        if not jp: continue  # no structured job data means no evidence the posting is live
        loc = jp.get("jobLocation"); loc = loc[0] if isinstance(loc, list) and loc else loc
        a = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
        out.append(dict(role=strip(jp.get("title") or title), url=clean(u), loc=", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry") if isinstance(a.get("addressCountry"), str) else "") if x),
                        opened=str(jp.get("datePosted") or "")[:10] or None, desc=strip(jp.get("description"))))
    return out
def _posted(s):  # "Posted 3 Days Ago" -> ISO date; "30+ days" is too vague to use
    s = (s or "").lower(); n = 0 if "today" in s else 1 if "yesterday" in s else None
    m = re.search(r"(\d+)(\+?)\s+days?", s)
    if m and not m.group(2): n = int(m.group(1))
    return (date.today() - timedelta(days=n)).isoformat() if n is not None else None
def a_workday(s):  # slug "tenant|wd3|SiteName"; uses the public job-search endpoint of Workday career sites
    t, wd, site = s.split("|"); base = f"https://{t}.{wd}.myworkdayjobs.com"; seen = {}
    for q in ("intern", "spring week"):
        for off in (0, 20, 40):
            d = json.loads(get(f"{base}/wday/cxs/{t}/{site}/jobs", 2_000_000, json.dumps({"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q}).encode())[2])
            for p in d.get("jobPostings", []): seen[p["externalPath"]] = p
            if len(d.get("jobPostings", [])) < 20: break
    out = []
    for path, p in seen.items():
        if not INC.search(p["title"]) or EXC.search(p["title"]): continue
        info = json.loads(get(f"{base}/wday/cxs/{t}/{site}{path}")[2]).get("jobPostingInfo", {})
        out.append(dict(role=p["title"], url=info.get("externalUrl") or f"{base}/en-US/{site}{path}", loc=p.get("locationsText", ""), opened=_posted(p.get("postedOn")), desc=strip(info.get("jobDescription"))))
    return out
def a_smartrecruiters(s):
    d = json.loads(get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=100&q=intern")[2]); out = []
    for p in d.get("content", []):
        if not INC.search(p["name"]) or EXC.search(p["name"]): continue
        x = json.loads(get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings/{p['id']}")[2]); l = p.get("location") or {}
        secs = (x.get("jobAd") or {}).get("sections") or {}
        out.append(dict(role=p["name"], url=x.get("postingUrl") or f"https://jobs.smartrecruiters.com/{s}/{p['id']}", loc=", ".join(v for v in (l.get("city"), l.get("country")) if v),
                        opened=(p.get("releasedDate") or "")[:10] or None, desc=strip(" ".join((v or {}).get("text", "") for v in secs.values()))))
    return out
def a_trackr(s):
    """Read Trackr's public programme feed; never requests LinkedIn URLs."""
    region, industry, season, kind = s.split("|")
    q = urlencode({"region": region, "industry": industry, "season": season, "type": kind})
    d = json.loads(get(f"https://api.the-trackr.com/programmes?{q}", 12_000_000)[2])
    out = []
    for p in d.get("programmes", []):
        u = clean(str(p.get("url") or ""))
        company = p.get("company") or {}
        if not u:
            careers = clean(str(company.get("careersSite") or ""))
            if careers.startswith("https://") and "linkedin." not in (urlparse(careers).hostname or "").lower():
                # Trackr sometimes has programme metadata but no individual posting URL.
                # Keep the employer's public careers destination, using a stable query
                # parameter so separate programmes do not overwrite one another.
                key = str(p.get("id") or p.get("slug") or p.get("name") or "programme")
                u = careers + ("&" if "?" in careers else "?") + "launchpad_trackr=" + re.sub(r"[^a-zA-Z0-9_-]", "-", key)[:80]
        host = (urlparse(u).hostname or "").lower()
        if not u.startswith("https://") or "linkedin." in host:
            continue
        cats = p.get("categories") or []
        locs = p.get("locations") or []
        out.append(dict(
            firm=strip(company.get("name")) or "Trackr listing",
            role=strip(p.get("name")),
            url=u,
            loc=", ".join(str(x) for x in locs if x),
            opened=str(p.get("openingDate") or "")[:10] or None,
            desc="; ".join(str(x) for x in (cats + [p.get("eligibility") or ""]) if x),
            trackr=True,
            trackr_fallback=not bool(p.get("url")),
        ))
    return out
ADAPTERS = {"greenhouse": a_greenhouse, "lever": a_lever, "ashby": a_ashby, "workday": a_workday, "smartrecruiters": a_smartrecruiters, "sitemap": a_sitemap, "trackr": a_trackr}
ATS_HINTS = [("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)"), ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"), ("lever", r"jobs\.lever\.co/([\w-]+)"),
             ("ashby", r"jobs\.ashbyhq\.com/([\w-]+)"), ("smartrecruiters", r"jobs\.smartrecruiters\.com/([\w-]+)"), ("workday", r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")]
def discover_html(h):
    """Find which ATS a careers page uses (static HTML only). Returns firms.json-style hints."""
    found = []
    for ats, rx in ATS_HINTS:
        for m in re.finditer(rx, h):
            slug = "|".join(m.groups()); e = {"ats": ats, "slug": slug}
            if e not in found and slug not in ("embed", "jobs"): found.append(e)
    return found

I = re.I
INC = re.compile(r"\b(intern(ship)?|spring\s*week|insight|summer\s+(analyst|placement|programme|program|vacation|scheme)|vacation\s+scheme)\b", I)
EXC = re.compile(r"graduate|apprentice|year[- ]in[- ]industry|industrial\s+placement|sandwich|placement\s+year|phd|doctoral|senior|principal|manager|permanent|work\s+experience|\b(12|9|6|4)[- ]?month", I)
RES = re.compile(r"\bwomen\b|female|black\s+heritage|social\s+mobility|first[- ]generation|underrepresented|under-represented", I)
DUR = re.compile(r"(?:duration|lasting|lasts?|for|of)\s*(?:approximately|around|about)?\s*(\d+)(?:\s*(?:-|to)\s*(\d+))?[- ]*(month|week)s?", I)
ELIG = re.compile(r"[^.]*\b(right to work|passport|visa|sponsor\w*|penultimate|final[- ]year|first[- ]year|second[- ]year|eligible)\b[^.]*\.?", I)
LVL = [("First year", r"first[- ]year|year 1\b"), ("Penultimate year", r"penultimate"), ("Final year", r"final[- ]year|graduating in")]
def ptype(t): return "Spring week" if re.search("spring", t, I) else "Insight" if re.search("insight", t, I) else "Off-cycle" if re.search(r"off[- ]cycle", t, I) else "Summer internship" if re.search("summer", t, I) else "Internship"
QUANT = re.compile(r"quant|trading|trader|market[- ]making", I)
CATS = {"eng": [("Aerospace", r"aero|aircraft|flight|propulsion|avionic|space|satellite|rocket"), ("Automotive", r"automotive|vehicle|motorsport"), ("Civil", r"civil|structur|geotech|highway|rail"),
        ("Electrical and Electronic", r"electr|embedded|\brf\b|hardware|power systems"), ("Chemical", r"chemical|process eng"), ("Nuclear", r"nuclear"), ("Materials", r"material"),
        ("Software", r"software|developer|data"), ("Mechanical", r"mechanical|manufactur|design eng"), ("Systems", r"systems")],
        "fin": [("Quant trading", r"trad"), ("Quant research", r"research"), ("Quant developer", r"develop|software|engineer")]}

def weeks(txt):
    w = 0
    for m in DUR.finditer(txt):
        n = max(int(m.group(1)), int(m.group(2) or 0)); w = max(w, n * (4.345 if m.group(3).lower() == "month" else 1))
    return w

def classify(firm, j):
    t, d = j["role"], j["desc"]
    if not INC.search(t) or EXC.search(t): return None, "not a short-term internship"
    if RES.search(t + " " + d[:800]): return None, "restricted programme"
    if weeks(t + " " + d) > 14: return None, "longer than 3 months"
    if firm["tab"] == "fin" and re.search(r"\bsummer\s+(intern(ship)?|analyst|programme|program|scheme)\b", t, I):
        return None, "summer internships belong in another tracker"
    if firm["tab"] == "fin" and not QUANT.search(t + " " + d):
        if not (firm.get("source") == "trackr" and re.search(r"spring|insight|discovery|immersion", t + " " + d, I)):
            return None, "not quant or trading"
    cat = next((c for c, rx in CATS[firm["tab"]] if re.search(rx, t, I)), None) or next((c for c, rx in CATS[firm["tab"]] if re.search(rx, d[:300], I)), "Other")
    m = ELIG.search(d); return dict(cat=cat, elig=(m.group(0).strip()[:180] if m else ""), level=", ".join(n for n, rx in LVL if re.search(rx, d, I)), ptype=ptype(t)), ""

SKIP = ("uk", "group", "plc", "ltd", "limited", "the")
def _words(n): return [w for w in re.sub(r"[^a-z0-9 ]", "", n.lower().replace("&", " and ").replace("-", " ")).split()]
def slugs(name):
    w = _words(name); core = [x for x in w if x not in SKIP]
    ex = list(dict.fromkeys(["".join(w), "-".join(w), "".join(core)]))
    return [(x, True) for x in ex if x] + ([(core[0], False)] if core and core[0] not in ex else [])
def _j(u):
    try: return json.loads(get(u)[2])
    except Exception: return None
def probe_one(name):
    """Find which public job board an employer uses by trying likely board names. Careful about look-alike companies."""
    t0 = (_words(name) or [""])[0]
    for slug, exact in slugs(name):
        d = _j(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if d and d.get("name") and t0 in re.sub(r"[^a-z0-9]", "", d["name"].lower()): return "greenhouse", slug
        time.sleep(.25)
        if not exact: continue
        d = _j(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1")
        if isinstance(d, list) and d: return "lever", slug
        time.sleep(.25); d = _j(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if d and d.get("jobs"): return "ashby", slug
        time.sleep(.25); d = _j(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
        if d and d.get("totalFound", 0) > 0: return "smartrecruiters", slug
        time.sleep(.25)
PROBED = os.path.join(HERE, "probed.json")
def probe(force=False):
    firms = json.load(open(FIRMS)); have = {f["name"] for f in firms}
    done = set(json.load(open(PROBED))) if os.path.exists(PROBED) and not force else set()
    for line in open(os.path.join(HERE, "firm_names.txt"), encoding="utf-8"):
        if "|" not in line: continue
        name, tab = line.strip().split("|")
        if name in have or name in done: continue
        r = probe_one(name); done.add(name); json.dump(sorted(done), open(PROBED, "w")); print(f"{name}: {r[0] + ' ' + r[1] if r else 'no public board found (Workday, Oracle or custom site)'}", flush=True)
        if r: firms.append({"name": name, "ats": r[0], "slug": r[1], "tab": tab}); json.dump(firms, open(FIRMS, "w"), indent=1)
    print("Employer search finished.", flush=True)

def _flat(d, p=""):
    o = {}
    for k, v in d.items():
        if isinstance(v, dict): o.update(_flat(v, p + k + "."))
        elif not isinstance(v, (list, tuple)): o[p + k] = v
    return o
def _records(x):
    best = []
    if isinstance(x, list):
        recs = [r for r in x if isinstance(r, dict)]
        if len(recs) >= 3 and any(re.search(r"url|link", k, I) for k in _flat(recs[0])): best = recs
        for r in x:
            c = _records(r); best = c if len(c) > len(best) else best
    elif isinstance(x, dict):
        for v in x.values():
            c = _records(v); best = c if len(c) > len(best) else best
    return best
def _d10(v):
    if isinstance(v, (int, float)) and v > 1e9: return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc).date().isoformat()
    return str(v)[:10] if re.match(r"\d{4}-\d{2}-\d{2}", str(v or "")) else None
def external_source(source):
    return str(source or "").startswith(("Imported", "Added by you"))
def ingest(rows, label):
    """Add rows from another source (old database, Trackr snapshot). Nothing is trusted: every row is rechecked live on sync."""
    rows = [_flat(r) for r in rows]; keys = list(dict.fromkeys(k for r in rows[:30] for k in r))
    pick = lambda *rxs: next((k for rx in rxs for k in keys if re.search(rx, k, I)), None)
    u = pick(r"apply.*(url|link)", r"^(url|link|href)$", r"(job|opportunity|posting|listing|application).*(url|link)", r"(url|link)$")
    ti = pick(r"(^|\.)(title|role|job_?title|programme|program)$") or next((k for k in keys if k.lower().endswith("name") and not re.search(r"company|employer|firm|organi|brand", k, I)), None)
    f, lo, dl, op, ca, vr = pick(r"company.*name", r"company|employer|firm|organi|brand"), pick(r"locat"), pick(r"deadline|clos"), pick(r"open|posted"), pick(r"sector|discipl|categ"), pick(r"verif|valid|live")
    print("Columns used:", dict(link=u, title=ti, firm=f, location=lo, deadline=dl, opened=op, category=ca, verified=vr))
    if not (u and ti): print("Could not find a link and a title field. Fields seen:", keys[:40]); return 0
    con, n, sk = db(), 0, dict(not_https=0, excluded_by_title=0, marked_invalid=0)
    for r in rows:
        url = clean(str(r.get(u) or "")); t = str(r.get(ti) or "")
        if not url.startswith("https://"): sk["not_https"] += 1; continue
        if EXC.search(t) or RES.search(t): sk["excluded_by_title"] += 1; continue
        if vr and str(r.get(vr)).strip().lower() in ("0", "false", "no", "none", "", "invalid", "dead", "closed", "failed", "rejected", "expired"): sk["marked_invalid"] += 1; continue
        cat = str(r.get(ca) or "") if ca else ""; tab = "fin" if re.search(r"quant|trad|financ", cat + " " + t, I) else "eng"
        con.execute("INSERT INTO listings(id,firm,role,tab,cat,loc,opened,deadline,rolling,url,elig,evidence,source,verified_at,active,level,ptype) VALUES(?,?,?,?,?,?,?,?,0,?,'','',?,?,1,'',?) ON CONFLICT(id) DO UPDATE SET firm=excluded.firm,role=excluded.role,tab=excluded.tab,cat=excluded.cat,loc=excluded.loc,opened=excluded.opened,deadline=excluded.deadline,url=excluded.url,source=excluded.source,verified_at=excluded.verified_at,active=1,ptype=excluded.ptype",
                    (hashlib.sha1(url.encode()).hexdigest()[:16], str(r.get(f) or "") if f else "", t, tab, cat or "Other", str(r.get(lo) or "") if lo else "", _d10(r.get(op)) if op else None, _d10(r.get(dl)) if dl else None, url,
                     f"Imported from {label}, link rechecked on sync", now(), ptype(t))); n += 1
    con.commit(); print(f"Imported {n} listings. Skipped: {sk}. They are rechecked live on the next sync.")
    if not n and rows: print("First row for diagnosis:", {k: str(v)[:50] for k, v in rows[0].items()})
    return n
def import_old(path):
    src = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True); src.row_factory = sqlite3.Row; best = []
    for (t,) in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        rows = [dict(r) for r in src.execute(f'SELECT * FROM "{t}"')]
        if len(rows) > len(best) and rows and any(re.search(r"url|link", k, I) for k in rows[0]): best = rows
    return ingest(best, "the previous app") if best else print("No table with a link column found.")
def import_json(path): return ingest(_records(json.load(open(path, encoding="utf-8"))), "a Trackr snapshot") if True else None

def public_host(h):
    try: return all(ipaddress.ip_address(a[4][0]).is_global for a in socket.getaddrinfo(h, 443))
    except Exception: return False
def _meta(h, p):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(p) + r'["\'][^>]*content=["\']([^"\']+)', h, I)
    return html.unescape(m.group(1)).strip() if m else ""
def add_url(url, tab):
    """Add one employer apply link you found elsewhere (for example from LinkedIn). The page is checked before anything is stored."""
    url = clean((url or "").strip()); p = urlparse(url)
    if tab not in ("eng", "fin"): return False, "Unknown tab."
    if p.scheme != "https" or not p.hostname: return False, "Only https links are accepted."
    if "linkedin." in p.hostname: return False, "Paste the employer's own apply link (the page the Apply button opens), not the LinkedIn link."
    if not public_host(p.hostname): return False, "That is not a public website address."
    try:
        if not allowed(url): return False, "That site's robots.txt does not allow automated reads."
        h = get(url)[2]
    except Exception as e: return False, f"Could not open the page ({str(e)[:80]})."
    ex = di.extract(h)
    if ex["closed_text"]: return False, "The page says this role is closed."
    jp = jobposting(h) or {}
    title = strip(jp.get("title")) or re.split(r"\s+\|\s+", _meta(h, "og:title"))[0]
    if not title: return False, "Could not read a job title from that page."
    org = jp.get("hiringOrganization"); firm = strip((org.get("name") if isinstance(org, dict) else org) or _meta(h, "og:site_name") or p.hostname)
    c, why = classify(dict(tab=tab), dict(role=title, desc=strip(jp.get("description")) or _meta(h, "og:description")))
    if not c: return False, f"Not added: {why}."
    rec = di.reconcile(di.from_ats(opens=str(jp.get("datePosted") or "")[:10] or None) + ex["candidates"])
    ok = lambda k: rec[k]["value"].isoformat() if rec[k] and rec[k]["confidence"] >= .6 else None
    loc = jp.get("jobLocation"); loc = loc[0] if isinstance(loc, list) and loc else loc; a = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
    ev = "; ".join(f'{k}: {rec[k]["evidence"]} ({rec[k]["source"]})' for k in ("opens", "closes") if rec[k]); con = db()
    con.execute("INSERT OR REPLACE INTO listings(id,firm,role,tab,cat,loc,opened,deadline,rolling,url,elig,evidence,source,verified_at,active,level,ptype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (hashlib.sha1(url.encode()).hexdigest()[:16], firm, title, tab, c["cat"], ", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry") if isinstance(a.get("addressCountry"), str) else "") if x), ok("opens"), ok("closes"),
                 int(ex["rolling"] and not ok("closes")), url, c["elig"], ev, "Added by you, page checked", now(), c["level"], c["ptype"])); con.commit()
    return True, f"Added: {firm}, {title}."

def recheck(con):
    """Re-verify imported listings: drop the dead or closed ones, keep them on temporary network errors."""
    for r in con.execute("SELECT id,url FROM listings WHERE active=1 AND (source LIKE 'Imported%' OR source LIKE 'Added by you%')").fetchall():
        try:
            if not allowed(r["url"]): continue
            s, _, h = get(r["url"]); ex = di.extract(h)
            if ex["closed_text"]: raise LookupError
            rec = di.reconcile(ex["candidates"]); ok = lambda k: rec[k]["value"].isoformat() if rec[k] and rec[k]["confidence"] >= .6 else None
            con.execute("UPDATE listings SET verified_at=?, opened=COALESCE(?,opened), deadline=COALESCE(?,deadline) WHERE id=?", (now(), ok("opens"), ok("closes"), r["id"]))
        except LookupError: con.execute("UPDATE listings SET active=0 WHERE id=?", (r["id"],))
        except urllib.error.HTTPError as e:
            if e.code in (404, 410): con.execute("UPDATE listings SET active=0 WHERE id=?", (r["id"],))
        except Exception: pass
        time.sleep(PAGE_DELAY)
    con.commit()

def db():
    c = sqlite3.connect(DB, timeout=30, isolation_level=None); c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript("""CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY,firm,role,tab,cat,loc,opened,deadline,rolling INT,url,elig,evidence,source,verified_at,active INT);
    CREATE TABLE IF NOT EXISTS health(firm TEXT PRIMARY KEY,ok INT,n INT,note,checked);
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY,v TEXT);""")
    have = {r[1] for r in c.execute("PRAGMA table_info(listings)")}
    for k in ("level", "ptype"):
        if k not in have: c.execute(f"ALTER TABLE listings ADD COLUMN {k}")
    return c

def sync_firm(con, f):
    name = f["name"]
    def health(ok, n, note): print(f"{name}: {note}", flush=True); con.execute("INSERT OR REPLACE INTO health VALUES(?,?,?,?,?)", (name, ok, n, note, now()))
    try:
        if f["ats"] == "trackr":
            if not re.fullmatch(r"(UK|US|EU|France|Germany|Italy|Hong Kong)\|(?:Finance|Tech|Law|Engineering)\|\d{4}\|[\w-]{1,60}", f["slug"]):
                raise ValueError("bad Trackr feed")
        elif not re.fullmatch(r"[\w.-]{1,80}(\|[\w-]{1,20}\|[\w-]{1,60})?", f["slug"]):
            raise ValueError("bad slug")
        jobs = ADAPTERS[f["ats"]](f["slug"])
    except Exception as e:  # upstream failure: keep the last known valid records
        return health(0, None, f"source unavailable, kept previous: {str(e)[:120]}")
    prev = con.execute("SELECT COUNT(*) FROM listings WHERE firm=? AND active=1", (name,)).fetchone()[0]
    if not jobs and prev: return health(0, prev, "empty feed, kept previous")
    keep, rej, closed = [], 0, []
    for j in jobs:
        j["url"] = clean(j["url"])
        c, why = classify(f, j)
        if not c: rej += 1; continue
        try:
            if not allowed(j["url"]): raise ValueError("robots.txt")
            s, final, h = get(j["url"])
            if s != 200: raise ValueError(f"HTTP {s}")
            time.sleep(PAGE_DELAY)
        except Exception: rej += 1; continue
        ex = di.extract(h); rec = di.reconcile(di.from_ats(opens=j["opened"]) + ex["candidates"])
        if di.status(rec, ex["rolling"], ex["closed_text"]) == "closed":
            closed.append(hashlib.sha1(j["url"].encode()).hexdigest()[:16]); rej += 1; continue
        ok = lambda k: rec[k]["value"].isoformat() if rec[k] and rec[k]["confidence"] >= .6 else None
        id_ = hashlib.sha1(j["url"].encode()).hexdigest()[:16]
        old = con.execute("SELECT opened,deadline FROM listings WHERE id=?", (id_,)).fetchone()
        o, d = ok("opens") or (old and old["opened"]), ok("closes") or (old and old["deadline"])
        ev = "; ".join(f'{k}: {rec[k]["evidence"]} ({rec[k]["source"]})' for k in ("opens", "closes") if rec[k])
        if f.get("source") == "trackr":
            source = "Trackr public feed, employer careers fallback" if j.get("trackr_fallback") else "Trackr public feed, link rechecked"
        else:
            source = f"Direct employer ({f['ats']}), link rechecked"
        con.execute("INSERT OR REPLACE INTO listings(id,firm,role,tab,cat,loc,opened,deadline,rolling,url,elig,evidence,source,verified_at,active,level,ptype) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (id_, j.get("firm") or name, j["role"], f["tab"], c["cat"], j["loc"], o, d, int(ex["rolling"] and not d), j["url"], c["elig"], ev, source, now(), c["level"], c["ptype"])); keep.append(id_)
    if keep:
        con.execute(f"UPDATE listings SET active=0 WHERE firm=? AND id NOT IN ({','.join('?' * len(keep))}) AND source NOT LIKE 'Imported%' AND source NOT LIKE 'Added by you%'", (name, *keep))
    elif closed:
        con.executemany("UPDATE listings SET active=0 WHERE id=?", ((id_,) for id_ in closed))
        health(0, 0, f"{len(jobs)} postings closed, previous closed links removed")
        return
    else:
        health(0, prev, "no verified postings, kept previous")
        return
    health(1, len(keep), f"{len(jobs)} postings, {rej} rejected")

def sync():
    if not LOCK.acquire(blocking=False): return False
    try:
        con = db(); firms = json.load(open(FIRMS)); SYNC.update(running=True, n=len(firms), i=0, now="")
        for k, f in enumerate(firms, 1): SYNC.update(i=k, now=f["name"]); sync_firm(con, f); con.commit()
        SYNC.update(now="rechecking saved links"); recheck(con)
        SYNC["last"] = now()
    finally: SYNC["running"] = False; LOCK.release()
    return True

CSP = "default-src 'none'; connect-src 'self'; img-src 'self'; manifest-src 'self'; font-src https://fonts.gstatic.com; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
STATIC = {"/": ("index.html", "text/html"), "/manifest.json": ("manifest.json", "application/json"), "/sw.js": ("sw.js", "text/javascript"), "/icon.svg": ("icon.svg", "image/svg+xml")}
class H(BaseHTTPRequestHandler):
    server_version = "Launchpad"
    def log_message(self, *a): pass
    def send(self, code, body, ctype="application/json", extra=""):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP + extra); self.end_headers(); self.wfile.write(b)
    def host_ok(self):
        # The app is safe to expose behind a hosting provider; mutation endpoints
        # still require the same-origin X-Launchpad header below.
        return bool(self.headers.get("Host", "").split(":")[0])
    def do_GET(self):
        if not self.host_ok(): return self.send(403, "{}")
        p = urlparse(self.path).path; con = db()
        if p in STATIC:
            f, ct = STATIC[p]; t = open(os.path.join(HERE, f), encoding="utf-8").read()
            n = secrets.token_urlsafe(16)
            if ct == "text/html":
                t = t.replace("<script>", f'<script nonce="{n}">').replace("<style>", f'<style nonce="{n}">')
                return self.send(200, t, ct, f"; script-src 'nonce-{n}'; style-src 'nonce-{n}' https://fonts.googleapis.com; style-src-attr 'unsafe-inline'")
            return self.send(200, t, ct)
        if p == "/api/listings":
            rows = con.execute("SELECT * FROM listings WHERE active=1").fetchall()
            return self.send(200, json.dumps([{**dict(r), "rolling": bool(r["rolling"])} for r in rows]))
        if p == "/api/health":
            return self.send(200, json.dumps(dict(sync=SYNC, firms=[dict(r) for r in con.execute("SELECT * FROM health ORDER BY firm")])))
        if p == "/api/state":
            r = con.execute("SELECT v FROM state WHERE k='s'").fetchone(); return self.send(200, r["v"] if r else "{}")
        self.send(404, "{}")
    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Launchpad") != "1": return self.send(403, "{}")
        p = urlparse(self.path).path
        if p == "/api/add":
            n = int(self.headers.get("Content-Length", 0))
            if n > 4000: return self.send(413, "{}")
            try: b = json.loads(self.rfile.read(n)); assert isinstance(b, dict)
            except Exception: return self.send(400, "{}")
            ok, msg = add_url(b.get("url"), b.get("tab")); return self.send(200, json.dumps({"ok": ok, "msg": msg}))
        if p == "/api/sync":
            threading.Thread(target=sync, daemon=True).start(); return self.send(202, "{}")
        if p == "/api/state":
            n = int(self.headers.get("Content-Length", 0))
            if n > 500_000: return self.send(413, "{}")
            try: s = json.loads(self.rfile.read(n)); assert isinstance(s, dict)
            except Exception: return self.send(400, "{}")
            con = db(); con.execute("INSERT OR REPLACE INTO state VALUES('s',?)", (json.dumps({"st": s.get("st", {}), "w": s.get("w", {})}),)); con.commit(); return self.send(200, "{}")
        self.send(404, "{}")

def loop():
    print("Syncing known employers, then searching for new ones...", flush=True); sync()
    SYNC.update(running=True, n=0, i=0, now="finding employer job boards"); probe(); SYNC["running"] = False
    while True: print("Sync started...", flush=True); sync(); print("Sync finished.", flush=True); time.sleep(1800)
if __name__ == "__main__":
    if "--probe" in sys.argv: probe(force=True); sys.exit()
    if "--import-json" in sys.argv: import_json(sys.argv[sys.argv.index("--import-json") + 1]); sys.exit()
    if "--import-old" in sys.argv: import_old(sys.argv[sys.argv.index("--import-old") + 1]); sys.exit()
    if "--discover" in sys.argv:  # python launchpad.py --discover https://firm.com/careers
        u = sys.argv[sys.argv.index("--discover") + 1]
        found = discover_html(get(u)[2]) if allowed(u) else []
        page = get(u)[2] if allowed(u) else ""
        if not found and re.search(r"phenompeople|phApp", page): found = [{"ats": "sitemap", "slug": urlparse(u).netloc}]
        print(json.dumps(found, indent=1) if found else "No supported ATS found in the static page. It may be JavaScript-rendered, Oracle, or a custom system."); sys.exit()
    port = int(os.environ.get("PORT", 8765)); db().close()
    threading.Thread(target=loop, daemon=True).start()
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Launchpad on http://{host}:{port}"); ThreadingHTTPServer((host, port), H).serve_forever()
