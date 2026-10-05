"""Listing collectors for Icelandic furniture sources.

Every collector returns a list of dicts:
  {source, title, price, url, image, text}
`text` is any extra description used for size parsing.
"""
from __future__ import annotations

import html as htmllib
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "is,en;q=0.8"})

PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s ]\d{3})+|\d{4,7})\s*(?:kr|ISK)", re.I)


def get(url, **kw):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2)


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(s, "html.parser").get_text(" ")).strip()


def parse_price(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = PRICE_RE.search(s)
    if not m:
        digits = re.sub(r"[^\d]", "", s.split(",")[0])
        return int(digits) if digits else None
    return int(re.sub(r"[^\d]", "", m.group(1)))


# --- Shopify ---------------------------------------------------------------

def shopify(name, domain, log):
    out, page = [], 1
    while page <= 40:
        r = get(f"https://{domain}/products.json", params={"limit": 250, "page": page})
        products = r.json().get("products", [])
        if not products:
            break
        for p in products:
            variants = [v for v in p.get("variants", []) if v.get("available", True)]
            if not variants:
                continue
            prices = [float(v["price"]) for v in variants]
            price = min(p_ for p_ in prices if p_ >= 0.3 * max(prices))
            img = (p.get("images") or [{}])[0].get("src")
            out.append({
                "source": name,
                "title": p["title"],
                "price": int(price),
                "url": f"https://{domain}/products/{p['handle']}",
                "image": img,
                "text": " ".join([p.get("product_type") or "", " ".join(p.get("tags") or []),
                                  strip_html(p.get("body_html"))]),
            })
        page += 1
    log(f"{name}: {len(out)} products in catalog")
    return out


# --- WooCommerce Store API ------------------------------------------------

def woocommerce(name, domain, log, terms=("borðstofuborð", "borðstofu", "eldhúsborð")):
    seen, out = set(), []
    for term in terms:
        page = 1
        while page <= 10:
            r = get(f"https://{domain}/wp-json/wc/store/v1/products",
                    params={"search": term, "per_page": 100, "page": page})
            items = r.json()
            if not isinstance(items, list) or not items:
                break
            for p in items:
                if p["id"] in seen or not p.get("is_in_stock", True):
                    continue
                seen.add(p["id"])
                pr = p.get("prices", {})
                minor = int(pr.get("currency_minor_unit") or 0)
                price = int(pr.get("price") or 0) // (10 ** minor) if pr.get("price") else None
                imgs = p.get("images") or []
                out.append({
                    "source": name,
                    "title": htmllib.unescape(p["name"]),
                    "price": price,
                    "url": p["permalink"],
                    "image": imgs[0]["src"] if imgs else None,
                    "text": strip_html(p.get("short_description")) + " " + strip_html(p.get("description")),
                })
            if len(items) < 100:
                break
            page += 1
    log(f"{name}: {len(out)} matching products")
    return out


# --- Generic HTML card extraction ------------------------------------------

def _img_src(img, base):
    for attr in ("data-src", "data-lazy-src", "src", "data-original"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return urljoin(base, v)
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        return urljoin(base, srcset.split(",")[0].split()[0])
    return None


BADGE_RE = re.compile(r"^(?:\d+\s*%|nýtt|vinsælt|fast lágt verð|sérpöntun|tilboð|útsala|outlet|sale|-)\s*", re.I)


def clean_title(t):
    t = PRICE_RE.split(t)[0] if PRICE_RE.search(t) else t
    t = re.split(r"\s(?:\d{1,3}(?:\.\d{3})+)\s*kr", t)[0]
    prev = None
    while prev != t:
        prev, t = t, BADGE_RE.sub("", t.strip())
    return t.strip(" -·|")


def extract_cards(html, base, link_pattern, source):
    soup = BeautifulSoup(html, "html.parser")
    cards = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if link_pattern not in href or href in cards:
            continue
        node, price = a, None
        for _ in range(7):
            # Stop once we've climbed out of this card into a list of several products
            links = {urljoin(base, x["href"]) for x in node.find_all("a", href=True)
                     if link_pattern in urljoin(base, x["href"])} if node is not a else {href}
            if len(links) > 1:
                break
            text = node.get_text(" ", strip=True)
            if PRICE_RE.search(text):
                # Sale cards show old + new price; the lower one is what you pay
                price = min(int(re.sub(r"[^\d]", "", x)) for x in PRICE_RE.findall(text))
                break
            if node.parent is None:
                break
            node = node.parent
        if price is None:
            continue
        card_text = node.get_text(" ", strip=True)
        title = clean_title(a.get_text(" ", strip=True))
        if len(title) < 4:
            h = node.find(["h2", "h3", "h4"]) if node else None
            img = node.find("img") if node else None
            title = (h.get_text(" ", strip=True) if h else "") or (img.get("alt", "") if img else "")
        img = node.find("img")
        cards[href] = {
            "source": source,
            "title": title,
            "price": price,
            "url": href,
            "image": _img_src(img, base) if img else None,
            "text": "",
            "sold_out": bool(re.search(r"uppsel", card_text, re.I)) and not re.search(r"til á|til í", card_text, re.I),
        }
    return list(cards.values())


def html_listing(name, urls, link_pattern, log):
    out = []
    for u in urls:
        r = get(u)
        found = extract_cards(r.text, u, link_pattern, name)
        out.extend(found)
    log(f"{name}: {len(out)} listings parsed from HTML")
    return out


# --- Bland.is ---------------------------------------------------------------

def bland(categories, pages, log):
    out = {}
    for cat in categories:
        for page in range(1, pages + 1):
            url = cat + (f"&page={page}" if page > 1 else "")
            try:
                r = get(url)
            except Exception as e:  # keep going on a bad page
                log(f"Bland: failed {url}: {e}")
                break
            cards = extract_cards(r.text, url, "/til-solu/", "Bland (notað)")
            if not cards:
                break
            for c in cards:
                out.setdefault(c["url"], c)
            time.sleep(1)
    log(f"Bland: {len(out)} listings scanned")
    return list(out.values())


def enrich_detail(item):
    """Fetch a listing page for description text and a better image (og:image)."""
    try:
        r = get(item["url"])
    except Exception:
        return item
    soup = BeautifulSoup(r.text, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        item["image"] = urljoin(item["url"], og["content"])
    desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
    body = soup.get_text(" ", strip=True)
    item["text"] = ((desc.get("content", "") if desc else "") + " " + body[:4000])
    return item
