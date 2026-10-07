"""Style matching with an OpenAI vision model.

Reference images live in refs/ (jpg/png/webp). Each candidate image is compared
against all references in one request and gets a 0-10 style score.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path

import requests

API = "https://api.openai.com/v1/chat/completions"
REFS = Path(__file__).resolve().parent.parent / "refs"

PROMPT = """You are helping someone find a {item} that matches a specific aesthetic.
The first {n} images are REFERENCE images showing the look they want.
The LAST image is a CANDIDATE listing ({title}).

In words, the target look is: {look}

Judge the candidate {item} only (ignore styling, other furniture, the room, photo quality):
materials and colour, wood tone, edges and joints, legs/base, back and seat (for seating), proportions,
overall design language. If the photo shows several pieces, judge the {item}.

Return JSON only:
{{"is_match": true|false (is the candidate actually a {item}?),
  "score": 0-10 (10 = would fit right in with the references, 5 = same broad category, 0 = opposite style),
  "materials": "short",
  "style": "2-5 words",
  "reason": "one sentence on why it matches or not"}}"""


def ref_dir(target_id):
    return REFS / target_id


def ref_files(target_id):
    return sorted(p for p in ref_dir(target_id).glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})


def ref_images(target_id):
    files = ref_files(target_id)
    out = []
    for p in files:
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        b64 = base64.b64encode(p.read_bytes()).decode()
        out.append(f"data:{mime};base64,{b64}")
    return out


def candidate_image(item):
    """Download the listing image ourselves (some shops block OpenAI's fetcher, some serve
    AVIF or mislabelled files) and re-encode it as a small JPEG."""
    import io
    from PIL import Image
    from .sources import get
    # Some sites (Bland) only serve images to requests that look like they come from their pages
    r = get(item["image"], headers={"Referer": item["url"],
                                    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8"})
    try:
        im = Image.open(io.BytesIO(r.content))
    except Exception:
        raise RuntimeError(f"not an image ({r.headers.get('content-type')}, {len(r.content)} bytes): "
                           f"{r.content[:120]!r}")
    im = im.convert("RGB")
    im.thumbnail((768, 768))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def score(item, refs, model, target, detail="low"):
    key = os.environ["OPENAI_API_KEY"]
    prompt = PROMPT.format(n=len(refs), title=item["title"], item=target["item"], look=target["look"].strip())
    content = [{"type": "text", "text": prompt}]
    for r in refs:
        content.append({"type": "image_url", "image_url": {"url": r, "detail": "low"}})
    content.append({"type": "image_url", "image_url": {"url": candidate_image(item), "detail": detail}})
    for attempt in range(6):
        resp = requests.post(
            API,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_object"},
                "max_tokens": 300,
            },
            timeout=90,
        )
        if resp.status_code != 429:
            break
        # Low-tier OpenAI accounts have a tokens-per-minute cap; wait it out
        m = re.search(r"try again in ([\d.]+)(ms|s)", resp.text)
        wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 10
        time.sleep(min(wait + 1, 60))
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI {resp.status_code}: {resp.text[:300]}")
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    return {
        "score": float(data.get("score", 0)),
        "is_match": bool(data.get("is_match", True)),
        "materials": data.get("materials", ""),
        "style": data.get("style", ""),
        "reason": data.get("reason", ""),
    }
