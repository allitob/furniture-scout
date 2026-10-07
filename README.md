# furniture-scout

Finds furniture in Icelandic shops and on Bland.is that matches a reference aesthetic. Current targets: **dining table** (≤ 400.000 kr, 140–190 cm) **dining chairs** (≤ 50.000 kr per chair, free allowed) and **sofas** (≤ 400.000 kr, free allowed).

**Latest results → [results.md](results.md)** — one page per item: [tables](results/dining-table.md) · [chairs](results/dining-chair.md) · [sofas](results/sofa.md)

## How it works

1. Collects listings daily (GitHub Actions, 07:52 UTC) from Epal, Snúran, Húsgagnahöllin, Línan, Módern, ILVA and Bland.is (used). IKEA.is renders its catalog with JavaScript, so it isn't covered.
2. Filters each target by keyword, price and (tables) length — see `targets` in `config.yaml`. Chair sets ("4 stólar", "6 stk") are priced per chair; Bland listings without a price are kept as gefins / no price.
3. Scores each photo against that target's images in `refs/<target>/` with an OpenAI vision model (0–10).
4. Writes `results/<target>.md` (one page per target, phone-friendly cards), an index in `results.md` and `data/<target>/results.csv`, and comments on the "New furniture matches" issue when a new listing scores ≥ 7 — that's the phone notification.

## Adjusting

- **Style:** add or remove images in `refs/dining-table/`, `refs/dining-chair/` or `refs/sofa/`. Changing a target's refs re-scores that target on the next run.
- **New target:** add an entry under `targets` in `config.yaml` and a matching `refs/<id>/` folder.
- **Budget, size, keywords, shop pages:** per target in `config.yaml`; shared sources at the bottom.
- **Model:** repo variable `OPENAI_MODEL` (Settings → Secrets and variables → Actions → Variables). Default `gpt-4o`.
- **Run now:** Actions → Furniture scout → Run workflow.

Requires repo secret `OPENAI_API_KEY`.
