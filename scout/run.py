"""Furniture Scout — one full run.

collect listings -> filter (keyword, price, size) -> score style vs refs/ ->
write results.md + data/results.csv -> notify on new strong matches.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
import time
import traceback
from pathlib import Path

import requests
import yaml

from . import filters, scoring, sources

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "listings.json"
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)
    if len(LOG) > 400:
        del LOG[:100]


def collect(cfg):
    src = cfg["sources"]
    jobs = []
    for s in src.get("shopify", []):
        jobs.append((s["name"], lambda s=s: sources.shopify(s["name"], s["domain"], log)))
    for s in src.get("woocommerce", []):
        jobs.append((s["name"], lambda s=s: sources.woocommerce(s["name"], s["domain"], log)))
    for s in src.get("html", []):
        jobs.append((s["name"], lambda s=s: sources.html_listing(s["name"], s["urls"], s["link_pattern"], log,
                                                                    broad=s.get("dining_category", False))))
    if src.get("bland"):
        b = src["bland"]
        jobs.append(("Bland", lambda: sources.bland(b["categories"], b["pages_per_category"], log)))

    items, health = [], {}
    for name, fn in jobs:
        try:
            got = fn()
            items.extend(got)
            health[name] = len(got)
        except Exception as e:
            log(f"{name}: FAILED — {type(e).__name__}: {e}")
            health[name] = f"failed: {type(e).__name__}"
    return items, health


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    scfg = cfg["search"]
    model = os.environ.get("OPENAI_MODEL") or cfg["scoring"]["model"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    DATA.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    raw, health = collect(cfg)

    # Used listings have vague titles ("Borð og 4 stólar"), so match more broadly there
    bland_cfg = dict(scfg, broad=True)
    cands = []
    for it in raw:
        c = bland_cfg if it["source"].startswith("Bland") or it.get("broad") else scfg
        if filters.is_candidate(it, c) and filters.price_ok(it, scfg):
            cands.append(it)
    log(f"{len(cands)} dining-table candidates within budget (from {len(raw)} listings)")

    # HTML cards (Bland, Módern, Línan...) carry little text: open each listing once for
    # description + full image, and reuse that on later runs
    fetched = 0
    for it in cands:
        if not it.get("from_html"):
            continue
        prev = state.get(it["url"], {})
        if prev.get("detail_text") is not None:
            it["text"] = prev["detail_text"]
            it["image"] = prev.get("image") or it.get("image")
        elif fetched < 150:
            sources.enrich_detail(it)
            it["detail_text"] = it.get("text", "")
            fetched += 1
            time.sleep(0.5)
    log(f"Opened {fetched} listing pages for details")

    kept = [it for it in cands if filters.size_ok(it, scfg) and it.get("image")]
    log(f"{len(kept)} pass the size filter ({scfg['min_length_cm']}-{scfg['max_length_cm']} cm)")

    refs = scoring.ref_images()
    can_score = bool(refs) and bool(os.environ.get("OPENAI_API_KEY"))
    if not refs:
        log("No reference images in refs/ — skipping style scoring")
    elif not os.environ.get("OPENAI_API_KEY"):
        log("OPENAI_API_KEY not set — skipping style scoring")
    refs_sig = str(len(refs)) + ":" + ",".join(sorted(p.name for p in scoring.REFS.glob("*")))

    budget = cfg["scoring"]["max_new_scores_per_run"]
    new_urls, scored_now = [], 0
    for it in kept:
        prev = state.get(it["url"], {})
        if not prev:
            new_urls.append(it["url"])
        rec = {**prev, **{k: it.get(k) for k in ("source", "title", "price", "url", "image", "size", "shape", "sold_out")}}
        if it.get("detail_text") is not None:
            rec["detail_text"] = it["detail_text"][:3000]
        rec.setdefault("first_seen", now)
        rec["last_seen"] = now
        if prev.get("price") and prev["price"] != it["price"]:
            rec["prev_price"] = prev["price"]
        needs_score = can_score and (prev.get("refs_sig") != refs_sig or "score" not in prev)
        if needs_score and scored_now < budget:
            try:
                rec.update(scoring.score(it, refs, model))
                rec["refs_sig"] = refs_sig
                scored_now += 1
            except Exception as e:
                log(f"score failed for {it['url']}: {e}")
                if "OpenAI 401" in str(e) or "OpenAI 404" in str(e):
                    can_score = False
        state[it["url"]] = rec
    log(f"Scored {scored_now} listings with {model}")

    live = {it["url"] for it in kept}
    for url, rec in state.items():
        rec["active"] = url in live
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    active = [r for r in state.values() if r["active"] and r.get("is_dining_table", True)]
    active.sort(key=lambda r: (-(r.get("score") or -1), r["price"]))
    write_csv(active)
    write_md(active, health, now, set(new_urls), model if refs else None)
    notify(active, set(new_urls), cfg["scoring"]["notify_min_score"])
    (DATA / "last_run.log").write_text("\n".join(LOG) + "\n", encoding="utf-8")


def write_csv(rows):
    cols = ["score", "title", "price", "size", "source", "style", "materials", "reason", "url", "image", "first_seen"]
    with open(DATA / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def fmt_isk(n):
    return f"{n:,}".replace(",", ".") + " kr"


def row_md(r, new):
    sc = r.get("score")
    score = f"**{sc:.0f}**/10" if sc is not None else "–"
    price = fmt_isk(r["price"])
    if r.get("prev_price") and r["prev_price"] > r["price"]:
        price += f"<br><sub>was {fmt_isk(r['prev_price'])}</sub>"
    badge = (" 🆕" if new else "") + (" · <sub>uppselt</sub>" if r.get("sold_out") else "")
    why = r.get("reason", "")
    img = f'<img src="{r["image"]}" width="140">' if r.get("image") else ""
    title = r["title"].replace("|", "/")
    return f"| {img} | {score} | [{title}]({r['url']}){badge}<br><sub>{r['source']} · {r['size']}</sub><br><sub>{why}</sub> | {price} |"


def write_md(rows, health, now, new, model):
    lines = [f"# Dining table matches", "",
             f"Updated {now}. {len(rows)} tables in budget and size range. Sorted by style match"
             + (f" ({model})." if model else " — add reference images to `refs/` to enable scoring."),
             "", "| | Match | Table | Price |", "|---|---|---|---|"]
    lines += [row_md(r, r["url"] in new) for r in rows[:60]]
    lines += ["", "<details><summary>Source status</summary>", ""]
    lines += [f"- {k}: {v}" for k, v in health.items()]
    lines += ["", "</details>", ""]
    (ROOT / "results.md").write_text("\n".join(lines), encoding="utf-8")


def notify(rows, new, min_score):
    hits = [r for r in rows if r["url"] in new and (r.get("score") or 0) >= min_score]
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not hits or not token or not repo:
        log(f"Notify: {len(hits)} new strong matches")
        return
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{repo}"
    title = "🪑 New furniture matches"
    issues = requests.get(f"{api}/issues", headers=h, params={"state": "open", "per_page": 50}).json()
    num = next((i["number"] for i in issues if i.get("title") == title), None)
    if num is None:
        num = requests.post(f"{api}/issues", headers=h, json={
            "title": title, "body": "New strong matches are posted here as comments.",
            "assignees": [repo.split("/")[0]]}).json()["number"]
    body = "\n".join(f"- **{r['score']:.0f}/10** [{r['title']}]({r['url']}) — {fmt_isk(r['price'])} · {r['source']}\n\n"
                     f"  <img src=\"{r['image']}\" width=\"200\">" for r in hits[:10])
    requests.post(f"{api}/issues/{num}/comments", headers=h, json={"body": body})
    log(f"Notify: posted {len(hits)} new matches to issue #{num}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
