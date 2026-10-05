# furniture-scout

Finds dining tables in Icelandic shops and on Bland.is that match a reference aesthetic.

**Latest results → [results.md](results.md)**

## How it works

1. Collects listings daily (GitHub Actions, 07:52 UTC) from Epal, Snúran, Húsgagnahöllin, Línan, Módern, ILVA and Bland.is (used). IKEA.is renders its catalog with JavaScript, so it isn't covered.
2. Filters by keyword, price and table length (`config.yaml`).
3. Scores each table's photo against the images in `refs/` with an OpenAI vision model (0–10).
4. Writes `results.md` and `data/results.csv`, and comments on the "New furniture matches" issue when a new listing scores ≥ 7 — that's the phone notification.

## Adjusting

- **Style:** add or remove images in `refs/`. Changing refs re-scores everything on the next run.
- **Budget, size, keywords, sources:** `config.yaml`.
- **Model:** repo variable `OPENAI_MODEL` (Settings → Secrets and variables → Actions → Variables). Default `gpt-4o`.
- **Run now:** Actions → Furniture scout → Run workflow.

Requires repo secret `OPENAI_API_KEY`.
