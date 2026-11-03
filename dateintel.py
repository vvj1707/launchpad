"""dateintel: evidence-based opening/closing date extraction. Standard library only.

Every date returned carries its kind, source, confidence and the exact text it came
from, so a wrong date can be traced and a stated date is never overwritten by a guess.
"""
import json, re
from datetime import date, timedelta
from html.parser import HTMLParser

M = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
PATTERNS = [  # (regex, kind)
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"), "iso"),
    (re.compile(r"(?<![\w/])(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + M + r"\b\.?,?(?:\s+(\d{4}))?", re.I), "dmy"),
    (re.compile(r"\b" + M + r"\b\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b,?(?:\s+(\d{4}))?", re.I), "mdy"),
    (re.compile(r"(?<![\d/.])(\d{1,2})[/.](\d{1,2})[/.](\d{4}|\d{2})(?![\d/.])"), "num"),
]
CLOSE = re.compile(r"(closing\s+date|application\s+deadline|deadline|apply\s+by|applications?\s+(?:will\s+)?close[sd]?(?:\s+(?:on|at))?|close\s+date|closes?(?:\s+on)?|closing(?:\s+on)?)", re.I)
OPEN = re.compile(r"(opening\s+date|open\s+date|applications?\s+(?:will\s+)?open(?:s|ed)?(?:\s+(?:on|from))?|date\s+posted|posted(?:\s+on)?|opens?(?:\s+on)?)", re.I)
ROLLING = re.compile(r"rolling\s+basis|rolling\s+(?:deadline|admission|recruitment|applications)|until\s+(?:the\s+)?(?:position|role|vacanc\w+|programme)\s+(?:is\s+)?filled|reviewed\s+as\s+(?:they\s+are\s+)?received|first[- ]come", re.I)
CLOSED = re.compile(r"no\s+longer\s+accepting|(?:position|role|vacancy)\s+(?:has\s+been|is)\s+(?:filled|closed)|applications?\s+(?:are|is|have)\s+(?:now\s+)?closed|this\s+(?:job|vacancy|opportunity)\s+(?:has\s+)?(?:closed|expired)|expired", re.I)
NOT_A_DEADLINE = re.compile(r"start|begin|commenc|run[s]?\b|programme\s+dates|duration|week\s+of", re.I)
RANK = {"ats_api": 0, "jsonld": 1, "employer_text": 2, "aggregator": 9}
BASE_CONF = {"ats_api": .9, "jsonld": .95, "employer_text": .75, "aggregator": .4}


def _mk(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_date(s, today, role="close"):
    """Earliest date in s -> (date, has_year, ambiguous) or None. UK day-first by default."""
    best = None
    for rx, kind in PATTERNS:
        m = rx.search(s)
        if not m or (best and m.start() >= best[0]):
            continue
        g = m.groups(); has_year, amb, d = True, False, None
        if kind == "iso":
            d = _mk(int(g[0]), int(g[1]), int(g[2]))
        elif kind in ("dmy", "mdy"):
            day, mon, yr = (g[0], g[1], g[2]) if kind == "dmy" else (g[1], g[0], g[2])
            mon = MONTHS[mon[:3].lower()]; day = int(day)
            if yr:
                d = _mk(int(yr), mon, day)
            else:
                has_year = False
                d = _mk(today.year, mon, day)
                if d and role == "close" and d < today - timedelta(days=14):
                    d = _mk(today.year + 1, mon, day)
                if d and role == "open" and d > today + timedelta(days=120):
                    d = _mk(today.year - 1, mon, day)
        else:
            a, b, yr = int(g[0]), int(g[1]), int(g[2]); yr = yr + 2000 if yr < 100 else yr
            if a > 12 and b <= 12: d = _mk(yr, b, a)
            elif b > 12 and a <= 12: d = _mk(yr, a, b)
            else: d, amb = _mk(yr, b, a), a <= 12 and b <= 12 and a != b
        if d:
            best = (m.start(), d, has_year, amb)
    return best[1:] if best else None


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "section", "dt", "dd"}
    def __init__(self):
        super().__init__(); self.out, self.ld, self._skip, self._ld = [], [], 0, False
    def handle_starttag(self, t, a):
        if t in ("script", "style", "noscript"):
            self._skip += 1
            self._ld = t == "script" and dict(a).get("type", "").lower() == "application/ld+json"
            self._buf = []
        if t in self.BLOCK: self.out.append("\n")
    def handle_endtag(self, t):
        if t in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
            if self._ld: self.ld.append("".join(self._buf)); self._ld = False
        if t in self.BLOCK: self.out.append("\n")
    def handle_data(self, d):
        if self._skip:
            if self._ld: self._buf.append(d)
        else: self.out.append(d)


def _jobposting(obj):
    if isinstance(obj, list):
        for o in obj: yield from _jobposting(o)
    elif isinstance(obj, dict):
        t = obj.get("@type"); t = t if isinstance(t, list) else [t]
        if "JobPosting" in t: yield obj
        for v in obj.get("@graph", []) if isinstance(obj.get("@graph"), list) else []: yield from _jobposting(v)


def extract(html, today=None, source="employer_text"):
    """-> {'candidates': [...], 'rolling': bool, 'closed_text': bool}. Pure, no network."""
    today = today or date.today()
    p = _Text(); p.feed(html)
    text = re.sub(r"[ \t\u00a0]+", " ", "".join(p.out))
    cands = []
    for blob in p.ld:
        try: data = json.loads(blob)
        except ValueError: continue
        for jp in _jobposting(data):
            for field, kind in (("datePosted", "opens"), ("validThrough", "closes")):
                v = str(jp.get(field) or "")[:10]
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", v)
                d = m and _mk(*map(int, m.groups()))
                if d: cands.append(dict(kind=kind, value=d, source="jsonld", confidence=BASE_CONF["jsonld"], evidence=f"JSON-LD {field}: {v}"))
    for rx, kind, role in ((CLOSE, "closes", "close"), (OPEN, "opens", "open")):
        for m in rx.finditer(text):
            win = text[m.end(): m.end() + 40].split("\n")[0]
            win = re.split(r"\.\s+[A-Z]", win)[0]
            r = find_date(win, today, role)
            if not r: continue
            d, has_year, amb = r
            before = win[: win.lower().find(str(d.day)) if str(d.day) in win else 0]
            if NOT_A_DEADLINE.search(before) or NOT_A_DEADLINE.search(m.group(0)): continue
            conf = BASE_CONF[source] - (0 if has_year else .3) - (.15 if amb else 0)
            cands.append(dict(kind=kind, value=d, source=source, confidence=round(conf, 2),
                              evidence=(m.group(0) + win)[:80].strip(), inferred_year=not has_year, ambiguous=amb))
    return dict(candidates=cands, rolling=bool(ROLLING.search(text)), closed_text=bool(CLOSED.search(text)))


def from_ats(opens=None, closes=None):
    """Wrap dates read from an ATS API field (ISO strings or date objects). Verify each field's meaning per ATS."""
    out = []
    for kind, v in (("opens", opens), ("closes", closes)):
        d = v if isinstance(v, date) else (_mk(*map(int, re.match(r"(\d{4})-(\d{2})-(\d{2})", v).groups())) if v else None)
        if d: out.append(dict(kind=kind, value=d, source="ats_api", confidence=BASE_CONF["ats_api"], evidence=f"ATS {kind}: {d}"))
    return out


def reconcile(cands, extra=()):
    """Pick one date per kind, never let a low-trust source beat a stated employer date, flag disagreements."""
    allc = list(cands) + list(extra); res = {"opens": None, "closes": None, "conflicts": []}
    for kind in ("opens", "closes"):
        cs = sorted((c for c in allc if c["kind"] == kind), key=lambda c: (-c["confidence"], RANK.get(c["source"], 5)))
        if not cs: continue
        top = cs[0]; res[kind] = top
        for c in cs[1:]:
            if c["value"] != top["value"] and c["confidence"] >= .6 and top["confidence"] >= .6:
                res["conflicts"].append(dict(kind=kind, chosen=top, other=c))
            elif c["value"] != top["value"] and c["source"] == "aggregator":
                res["conflicts"].append(dict(kind=kind, chosen=top, other=c, minor=True))
    res["needs_review"] = any(not c.get("minor") for c in res["conflicts"]) or any(
        (res[k] or {}).get("confidence", 1) < .6 for k in ("opens", "closes") if res[k])
    return res


def status(rec, rolling, closed_text, today=None):
    today = today or date.today(); c = rec.get("closes")
    if closed_text or (c and c["value"] < today): return "closed"
    o = rec.get("opens")
    if o and o["value"] > today: return "upcoming"
    return "rolling" if rolling and not c else "open"
