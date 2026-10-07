"""Keyword, price, unit-count and size filtering. Everything item-specific comes from the target config."""
from __future__ import annotations

import re
import unicodedata

NUM = r"(\d{2,3})"
# 180x90, 180 x 90 cm, 160/220x90 (extendable), 160-260 x 90
RECT_RE = re.compile(NUM + r"(?:\s*(?:/|-|–|til)\s*" + NUM + r")?\s*(?:cm)?\s*[x×*]\s*" + NUM)
ROUND_RE = re.compile(r"(?:ø|Ø|⌀|þvermál\s*:?\s*)\s*" + NUM)
LENGTH_RE = re.compile(r"(?:lengd|length|l)\s*[:.]?\s*" + NUM + r"(?:\s*(?:/|-|–|til)\s*" + NUM + r")?\s*cm", re.I)


def norm(s: str) -> str:
    return (s or "").lower()


def ascii_fold(s: str) -> str:
    s = norm(s).replace("ð", "d").replace("þ", "th").replace("æ", "ae")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _in(words, text, folded):
    return any(k in text or ascii_fold(k) in folded for k in words)


def is_candidate(item, t, broad=False) -> bool:
    """Decide from the TITLE whether this listing is the target item; descriptions only break ties.
    `broad` = the listing comes from a page that is already about this item (or from Bland, whose titles are vague)."""
    title = norm(item["title"])
    folded_title = ascii_fold(item["title"])
    hay = title + " " + norm(item.get("text", ""))[:600]
    folded = ascii_fold(hay)
    if _in(t["exclude_keywords"], title, folded_title):
        return False
    rej, unless = t.get("reject_title_re"), t.get("unless_title_re")
    if rej and re.search(rej, title) and not (unless and re.search(unless, title)):
        return False
    if _in(t["include_keywords"], title, folded_title):
        return True
    gen = t.get("generic_title_re")
    if gen and (re.search(gen, title) or re.search(gen, folded_title)):
        return broad or _in(t["include_keywords"] + t.get("context_keywords", []), hay, folded)
    return False


# --- Sets of several units ("4 stólar", "6 stk", "par af stólum") -------------

NUM_WORDS = {
    "tveir": 2, "tvo": 2, "tvær": 2, "tvaer": 2, "par": 2, "two": 2, "pair": 2,
    "þrír": 3, "þrjá": 3, "þrjár": 3, "thrir": 3, "three": 3,
    "fjórir": 4, "fjóra": 4, "fjórar": 4, "fjorir": 4, "four": 4,
    "fimm": 5, "five": 5, "sex": 6, "six": 6, "sjö": 7, "seven": 7, "átta": 8, "atta": 8, "eight": 8,
    "tíu": 10, "ten": 10,
}
_NW = "|".join(sorted(NUM_WORDS, key=len, reverse=True))
_N = r"(\d{1,2}|" + _NW + r")"
COUNT_RES = [
    re.compile(_N + r"\s*(?:x\s*)?(?:stk\b|stk\.|stykki|st[oó]l(?:ar|a|um)?\b|chairs\b|eins\b|samst)", re.I),
    re.compile(r"(?:sett af|set of|par af)\s*" + _N, re.I),
    re.compile(r"(?:^|\s)(\d{1,2})\s*x\s+[a-záéíóúýþæöð]", re.I),  # "4x Ami stóll" (not 160x90)
    re.compile(r"st[oó]la(par)\b", re.I),  # "stólapar"
    re.compile(r"\b(par)\s+(?:af\s+)?st[oó]l", re.I),  # "par af stólum"
]
PER_UNIT_RE = re.compile(r"(?:verð\s*)?(?:per|pr\.?)\s*(?:stk|st[oó]l|stykki)|stykkið|stykkid|\bhver st[oó]ll|\beach\b|á stykki|/\s*stk", re.I)
PLURAL_RE = re.compile(r"st[oó]lar\b|\bchairs\b|\bstk\b", re.I)
FREE_RE = re.compile(r"gefins|ókeypis|okeypis|frítt|fritt|\bfree\b|fæst fyrir ekkert", re.I)


def unit_count(*texts) -> int:
    """How many units the listed price covers. Explicit "per stk" wins; otherwise the first count found."""
    for t in texts:
        if t and PER_UNIT_RE.search(t):
            return 1
    for t in texts:
        if not t:
            continue
        for rx in COUNT_RES:
            m = rx.search(t)
            if m:
                g = m.group(1).lower()
                n = 2 if g == "par" else NUM_WORDS.get(g) or (int(g) if g.isdigit() else 0)
                if 1 <= n <= 12:
                    return n
    return 1


def looks_like_set(title) -> bool:
    return bool(PLURAL_RE.search(title or ""))


def set_unit_price(item, t, desc=""):
    """Annotate item with units / unit_price for per-unit targets."""
    if not t.get("per_unit"):
        item["units"], item["unit_price"] = 1, item.get("price")
        return
    n = unit_count(item["title"], desc[:500])
    item["units"] = n
    p = item.get("price")
    item["unit_price"] = None if p is None else round(p / n)


def price_ok(item, t, lenient=False):
    """`lenient`: details not fetched yet and the title suggests a set — let it through so the
    description can be read for the count; the real check runs again afterwards."""
    p = item.get("unit_price", item.get("price"))
    if p is None:
        return False
    hi = t["max_price_isk"] * (12 if lenient else 1)
    return t["min_price_isk"] <= p <= hi


# --- Size (tables) ---------------------------------------------------------------

def parse_size(text: str):
    """Return (shape, lengths) — lengths is a list of candidate top lengths in cm."""
    t = text or ""
    rects = []
    for a, b, c in RECT_RE.findall(t):
        a, c = int(a), int(c)
        b = int(b) if b else None
        if not (60 <= a <= 400 and 40 <= c <= 400):
            continue
        # The first number is usually length; with an extension range use both
        lens = [a] + ([b] if b else [])
        width = c
        if max(lens) < width:  # written as width x length
            lens, width = [width], max(lens)
        rects.append((lens, width))
    if rects:
        return "rect", sorted({l for lens, _ in rects for l in lens})
    for a, b in LENGTH_RE.findall(t):
        return "rect", [int(a)] + ([int(b)] if b else [])
    m = re.search(r"\bL\s?(\d{2,3})\b", t)  # "SC113 L160"
    if m:
        return "rect", [int(m.group(1))]
    m = ROUND_RE.search(t)
    if m:
        return "round", [int(m.group(1))]
    return None, []


def size_ok(item, t):
    cfg = t.get("size")
    if not cfg:  # no size rule for this target
        item.setdefault("size", "")
        item.setdefault("shape", None)
        return True
    shape, lens = parse_size(item["title"] + " " + item.get("text", ""))
    if ROUND_RE.search(item["title"]) and not RECT_RE.search(item["title"]):
        shape, lens = "round", [int(ROUND_RE.search(item["title"]).group(1))]
    item["shape"] = shape
    if shape == "round":
        item["size"] = f"Ø{lens[0]}"
        return cfg["allow_round"]
    if not lens:
        item["size"] = "size?"
        return cfg["keep_unknown_size"]
    item["size"] = "/".join(str(l) for l in lens) + " cm"
    lo, hi = cfg["min_length_cm"], cfg["max_length_cm"]
    if len(lens) >= 2:  # extendable: the range must reach into [lo, hi]
        return min(lens) <= hi and max(lens) >= lo
    return lo <= lens[0] <= hi
