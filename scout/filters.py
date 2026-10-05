"""Keyword, price and size filtering."""
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


def is_candidate(item, cfg) -> bool:
    title = norm(item["title"])
    folded_title = ascii_fold(item["title"])
    hay = title + " " + norm(item.get("text", ""))[:600]
    folded = ascii_fold(hay)
    if any(k in title or ascii_fold(k) in folded_title for k in cfg["exclude_keywords"]):
        return False
    # A chair listing that mentions tables in passing is still a chair; a "table + chairs" set is kept
    if re.search(r"st[oó]l|chair", title) and not re.search(r"bor[dð]|table", title):
        return False
    return any(k in hay or ascii_fold(k) in folded for k in cfg["include_keywords"])


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
    m = ROUND_RE.search(t)
    if m:
        return "round", [int(m.group(1))]
    return None, []


def size_ok(item, cfg):
    shape, lens = parse_size(item["title"] + " " + item.get("text", ""))
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


def price_ok(item, cfg):
    p = item.get("price")
    return p is not None and cfg["min_price_isk"] <= p <= cfg["max_price_isk"]
