"""Furniture Scout — one full run.

collect listings (shared + per target) -> for each target: filter (keyword, price, size) ->
score style vs refs/<target>/ -> write results.md + data/<target>/results.csv -> notify on new strong matches.
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
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)
    if len(LOG) > 600:
        del LOG[:100]


def _run(name, fn, health):
    try:
        got = fn()
        health[name] = len(got)
        return got
    except Exception as e:
        log(f"{name}: FAILED — {type(e).__name__}: {e}")
        health[name] = f"failed: {type(e).__name__}"
        return []


def collect_shared(src, health):
    items = []
    for s in src.get("shopify", []):
        items += _run(s["name"], lambda s=s: sources.shopify(s["name"], s["domain"], log), health)
    if src.get("bland"):
        b = src["bland"]
        items += _run("Bland", lambda: sources.bland(b["categories"], b["pages_per_category"], log), health)
    return items


def collect_target(t, src, health):
    items = []
    for s in src.get("woocommerce", []):
        terms = tuple(t.get("woocommerce_terms") or ())
        if terms:
            items += _run(f"{s['name']} [{t['id']}]",
                          lambda s=s: sources.woocommerce(s["name"], s["domain"], log, terms=terms), health)
    for s in t.get("html", []):
        items += _run(f"{s['name']} [{t['id']}]",
                      lambda s=s: sources.html_listing(s["name"], s["urls"], s["link_pattern"], log,
                                                       broad=s.get("dining_category", False)), health)
    return items


def state_path(tid):
    return DATA / tid / "listings.json"


def process_target(t, shared, cfg, model, now, health):
    tid = t["id"]
    (DATA / tid).mkdir(parents=True, exist_ok=True)
    sp = state_path(tid)
    state = json.loads(sp.read_text()) if sp.exists() else {}

    # Own copies: targets annotate items differently (size, units)
    raw = [dict(it) for it in shared] + collect_target(t, cfg["sources"], health)

    cands = []
    for it in raw:
        # Used listings have vague titles ("Borð og 4 stólar"), so match more broadly there
        broad = it["source"].startswith("Bland") or it.get("broad", False)
        if not filters.is_candidate(it, t, broad=broad):
            continue
        filters.set_unit_price(it, t)
        lenient = t.get("per_unit") and it.get("from_html") and it["units"] == 1 and filters.looks_like_set(it["title"])
        if filters.price_ok(it, t, lenient=lenient):
            cands.append(it)
    log(f"[{tid}] {len(cands)} candidates within budget (from {len(raw)} listings)")

    # HTML cards (Bland, Módern, Línan...) carry little text: open each listing once for
    # description + full image, and reuse that on later runs
    fetched = 0
    for it in cands:
        if not it.get("from_html"):
            continue
        prev = state.get(it["url"], {})
        if prev.get("detail_text") is not None:
            it["text"] = prev["detail_text"]
            it["desc"] = prev.get("detail_desc", "")
            it["image"] = sources.fix_image_url(prev.get("image") or it.get("image"))
            if it.get("no_price") and prev.get("price") and not prev.get("no_price"):
                it["price"], it["no_price"] = prev["price"], False
        elif fetched < 150:
            sources.enrich_detail(it)
            it["detail_text"] = it.get("text", "")
            fetched += 1
            time.sleep(0.5)
    log(f"[{tid}] Opened {fetched} listing pages for details")

    kept = []
    for it in cands:
        filters.set_unit_price(it, t, it.get("desc", ""))
        if it.get("no_price") and filters.FREE_RE.search(it["title"] + " " + it.get("desc", "")):
            it["free"] = True
        if filters.price_ok(it, t) and filters.size_ok(it, t) and it.get("image"):
            kept.append(it)
    log(f"[{tid}] {len(kept)} pass price/size filters")

    refs = scoring.ref_images(tid)
    can_score = bool(refs) and bool(os.environ.get("OPENAI_API_KEY"))
    if not refs:
        log(f"[{tid}] No reference images in refs/{tid}/ — skipping style scoring")
    elif not os.environ.get("OPENAI_API_KEY"):
        log("OPENAI_API_KEY not set — skipping style scoring")
    refs_sig = str(len(refs)) + ":" + ",".join(p.name for p in scoring.ref_files(tid))

    budget = cfg["scoring"]["max_new_scores_per_run"]
    new_urls, scored_now = [], 0
    keys = ("source", "title", "price", "url", "image", "size", "shape", "sold_out", "units", "unit_price", "no_price", "free")
    for it in kept:
        prev = state.get(it["url"], {})
        if not prev:
            new_urls.append(it["url"])
        rec = {**prev, **{k: it.get(k) for k in keys}}
        if it.get("detail_text") is not None:
            rec["detail_text"] = it["detail_text"][:3000]
            rec["detail_desc"] = it.get("desc", "")[:1000]
        rec.setdefault("first_seen", now)
        rec["last_seen"] = now
        if prev.get("price") and prev["price"] != it["price"]:
            rec["prev_price"] = prev["price"]
        needs_score = can_score and (prev.get("refs_sig") != refs_sig or "score" not in prev)
        if needs_score and scored_now < budget:
            try:
                rec.update(scoring.score(it, refs, model, t))
                rec["refs_sig"] = refs_sig
                scored_now += 1
            except Exception as e:
                log(f"[{tid}] score failed for {it['url']}: {e}")
                if "OpenAI 401" in str(e) or "OpenAI 404" in str(e):
                    can_score = False
        state[it["url"]] = rec
    log(f"[{tid}] Scored {scored_now} listings with {model}")

    live = {it["url"] for it in kept}
    for url, rec in state.items():
        rec["active"] = url in live
    sp.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    active = [r for r in state.values() if r["active"] and r.get("is_match", r.get("is_dining_table", True))]
    active.sort(key=lambda r: (-(r.get("score") or -1), r.get("unit_price") or r["price"]))
    write_csv(tid, active)
    return active, set(new_urls), bool(refs)


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    model = os.environ.get("OPENAI_MODEL") or cfg["scoring"]["model"]
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    DATA.mkdir(exist_ok=True)

    health = {}
    shared = collect_shared(cfg["sources"], health)
    sections = []
    for t in cfg["targets"]:
        try:
            active, new, has_refs = process_target(t, shared, cfg, model, now, health)
        except Exception as e:
            log(f"[{t['id']}] FAILED — {type(e).__name__}: {e}")
            traceback.print_exc()
            continue
        sections.append((t, active, new, has_refs))
        notify(t, active, new, cfg["scoring"]["notify_min_score"])
    write_md(sections, health, now, model)
    (DATA / "last_run.log").write_text("\n".join(LOG) + "\n", encoding="utf-8")


def write_csv(tid, rows):
    cols = ["score", "title", "price", "units", "unit_price", "size", "source", "style", "materials", "reason",
            "url", "image", "first_seen"]
    with open(DATA / tid / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def fmt_isk(n):
    return f"{n:,}".replace(",", ".") + " kr"


def price_md(r):
    if r.get("no_price"):
        return "gefins" if r.get("free") else "no price<br><sub>free or offer — check</sub>"
    s = fmt_isk(r["price"])
    if (r.get("units") or 1) > 1:
        s += f"<br><sub>{r['units']} stk · {fmt_isk(r['unit_price'])}/stk</sub>"
    if r.get("prev_price") and r["prev_price"] > r["price"]:
        s += f"<br><sub>was {fmt_isk(r['prev_price'])}</sub>"
    return s


def row_md(r, new):
    sc = r.get("score")
    score = f"**{sc:.0f}**/10" if sc is not None else "–"
    badge = (" 🆕" if new else "") + (" · <sub>uppselt</sub>" if r.get("sold_out") else "")
    why = r.get("reason", "")
    img = f'<img src="{r["image"]}" width="140">' if r.get("image") else ""
    title = r["title"].replace("|", "/")
    meta = r["source"] + (f" · {r['size']}" if r.get("size") else "")
    return f"| {img} | {score} | [{title}]({r['url']}){badge}<br><sub>{meta}</sub><br><sub>{why}</sub> | {price_md(r)} |"


def write_md(sections, health, now, model):
    lines = ["# Furniture matches", "", f"Updated {now}. Sorted by style match ({model}).", ""]
    lines += [f"- [{t['item'].capitalize()}](#{t['item'].replace(' ', '-')}s) — {len(rows)} in budget"
              for t, rows, _, _ in sections]
    for t, rows, new, has_refs in sections:
        price = (f"{fmt_isk(t['min_price_isk'])}–{fmt_isk(t['max_price_isk'])}"
                 + (" per piece" if t.get("per_unit") else ""))
        lines += ["", f"## {t['item'].capitalize()}s", "",
                  f"{len(rows)} listings · {price}"
                  + ("" if has_refs else f" · add reference images to `refs/{t['id']}/` to enable scoring"),
                  "", "| | Match | Item | Price |", "|---|---|---|---|"]
        lines += [row_md(r, r["url"] in new) for r in rows[:60]]
    lines += ["", "<details><summary>Source status</summary>", ""]
    lines += [f"- {k}: {v}" for k, v in health.items()]
    lines += ["", "</details>", ""]
    (ROOT / "results.md").write_text("\n".join(lines), encoding="utf-8")


def notify(t, rows, new, min_score):
    hits = [r for r in rows if r["url"] in new and (r.get("score") or 0) >= min_score]
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not hits or not token or not repo:
        log(f"[{t['id']}] Notify: {len(hits)} new strong matches")
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
    body = f"**{t['item'].capitalize()}s**\n\n" + "\n".join(
        f"- **{r['score']:.0f}/10** [{r['title']}]({r['url']}) — {price_md(r).replace('<br>', ' ')} · {r['source']}\n\n"
        f"  <img src=\"{r['image']}\" width=\"200\">" for r in hits[:10])
    requests.post(f"{api}/issues/{num}/comments", headers=h, json={"body": body})
    log(f"[{t['id']}] Notify: posted {len(hits)} new matches to issue #{num}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
