# Collect AI News (Bi-Weekly)

## Objective
Collect AI-related news from X/Twitter (the only source), summarize and translate to Chinese using a local Ollama model, and output a formatted Word document with two sections: (1) AI News table and (2) AI Fundraising News table sourced from a live ChatGPT web search.

## Required Inputs
- **Start date** (YYYY-MM-DD)
- **End date** (YYYY-MM-DD)
- **Local Ollama** running with a pulled model (no API key, no cost):
  - Install: https://ollama.com
  - `ollama pull qwen2.5:32b-instruct-q4_K_M` (default model — swap via `OLLAMA_MODEL` in `.env` for a smaller/faster or larger/higher-quality model)
  - Used for: article summarization, relevance scoring, Chinese translation, executive summary, and funding extraction from collected posts
- **API keys** in `.env`:
  - `OPENAI_API_KEY` — for AI funding news search (ChatGPT with web search). Optional; the funding section is skipped without it.
- **X/Twitter source** (no API key required — uses free Nitter RSS):
  - `x_accounts.txt` - handles + search queries to follow (repo root, editable)
  - `NITTER_INSTANCES` in `.env` (optional) - comma-separated Nitter instances to override the built-in defaults when they are down

## Tools Required
1. `tools/collect_x.py` - X/Twitter posts via Nitter RSS (accounts + searches from `x_accounts.txt`)
2. `tools/summarize_articles.py` - Fetch full content and generate summaries via local Ollama
3. `tools/generate_word_doc.py` - Word document generation with translation and funding section
4. `tools/utils.py` - Shared utilities (imported by other tools; also hosts the `call_ollama` / `translate_to_chinese_ollama` helpers)

## Steps

### Phase 1: User Input
1. **Prompt user for date range:**
   ```
   Enter start date (YYYY-MM-DD): 2026-07-06
   Enter end date (YYYY-MM-DD): 2026-07-20
   ```

2. **Validate dates** using `utils.validate_date_range()`
   - Must be in YYYY-MM-DD format
   - Start date must be before end date
   - Range cannot exceed 1 year

### Phase 2: Data Collection

3. **Collect X/Twitter posts:**
   ```bash
   python tools/collect_x.py --start_date 2026-07-06 --end_date 2026-07-20
   ```
   - Output: `.tmp/raw_x.json`
   - Reads accounts + search queries from `x_accounts.txt` (edit that file to change who we follow)
   - Uses **free Nitter RSS** — no API key. Account timelines are reliable; Nitter search is often disabled (funding `search:` lines are best-effort)
   - Full tweet text is captured into `content`/`description` so the summarizer can score it without scraping x.com (which is blocked)
   - Retweets are skipped; posts are filtered to the date range
   - Expected: 50-150 posts per 2-week range, but **0 is possible if all Nitter instances are down/rate-limited** — that's expected, not a failure. Re-run later or set `NITTER_INSTANCES` in `.env`

### Phase 3: Deduplication

4. **Deduplicate articles** (inline Python — no dedicated script):
   ```python
   import json

   with open('.tmp/raw_x.json') as f: x_posts = json.load(f)

   seen, unique = set(), []
   for a in x_posts:
       url = a.get('url', '')
       if url and url not in seen:
           seen.add(url)
           unique.append(a)

   with open('.tmp/classified_articles.json', 'w') as f:
       json.dump(unique, f, ensure_ascii=False, indent=2)
   ```
   - Output: `.tmp/classified_articles.json`

### Phase 4: Article Summarization

5. **Summarize collected posts:**
   ```bash
   python tools/summarize_articles.py --provider ollama --yes
   ```
   - Input: `.tmp/classified_articles.json`
   - Uses the **local Ollama model** (`qwen2.5:32b-instruct-q4_K_M` by default) to generate concise paragraph summaries, category, relevance score, and VC signal type
   - X posts already have full tweet text captured at collection time, so no fetch is needed — the tweet text is summarized/scored directly
   - Output: `.tmp/summarized_articles.json`
   - Cost: $0.00 (runs entirely locally)
   - Estimated time: depends on local hardware — expect roughly 2-5s per post once the model is warm (first call after idle pays a one-time model-load cost, often 10s+)

### Phase 5: Word Document Generation

6. **Generate Word document with Chinese translation and funding section:**
   ```bash
   python tools/generate_word_doc.py --start_date 2026-07-06 --end_date 2026-07-20 \
     --articles .tmp/summarized_articles.json --translate
   ```
   - Input: `.tmp/summarized_articles.json`
   - **Section 0 — Watchlist Highlights** (optional, appears if `watchlist.txt` has entries):
     - Lists any articles mentioning companies in `watchlist.txt`, sorted by relevance
     - Edit `watchlist.txt` (one company per line) to track investment targets
   - **Section 1 — AI News table** (3 columns when categorized):
     - **Date** | **优先级 (1-5)** | **Summary**: hyperlinked title + VC signal badge + Chinese/English paragraphs
     - VC signal badges: `[融资]` `[产品]` `[合作]` `[人事]` `[监管]` `[研究]` — colored by type
     - Articles grouped by category, sorted by relevance (highest first) within each group
     - Filter low-signal noise: `--min-signal 3` drops relevance 1-2 articles
   - **Section 2 — AI Fundraising News table** (8 columns):
     - Columns: Date | Company | 优先级 | Summary | Stage | Raise | Valuation | Investors
     - **Primary source**: local Ollama extracts structured funding events from collected posts
     - **Supplemental source**: ChatGPT (`gpt-4o-search-preview`) live web search fills gaps (skipped entirely if `OPENAI_API_KEY` is unset)
     - Two sources are merged and deduplicated by company name (richer entry wins)
   - Output: `output/AI_News_20260706_20260720.docx`
   - Estimated cost: $0.00 for summarization/translation/funding-extraction (Ollama) + ~$0.05-0.10 OpenAI funding web search (optional)

   **Flags:**
   - `--min-signal N` — only include articles with relevance ≥ N (1=all, 3=curated, 4=high-signal only)
   - `--watchlist FILE` — path to watchlist file (default: `watchlist.txt`)

## Expected Outputs

**Primary Deliverable:**
- Word document: `output/AI_News_[start]_[end].docx`
  - **AI News Summary** table: all posts grouped by category, 3 columns (Date | 优先级 | Chinese summary with hyperlinked title)
  - **AI Fundraising News** table: funding events extracted locally + supplemented via ChatGPT web search

**Intermediate Files (in `.tmp/`):**
- `raw_x.json` - X/Twitter posts collected via Nitter RSS (source `X/Twitter`)
- `classified_articles.json` - Deduplicated posts
- `summarized_articles.json` - Posts with Ollama-generated summaries

## Edge Cases & Error Handling

### Ollama

**Ollama unreachable:**
- **Symptom**: `WARNING: Could not reach Ollama at http://localhost:11434 — is 'ollama serve' running?`
- **Solution**: Start Ollama (`ollama serve`, or just open the Ollama app) before running the pipeline
- **Prevention**: On macOS/Windows the Ollama app runs the server automatically once installed

**Model not pulled:**
- **Symptom**: Ollama call fails with a model-not-found error
- **Solution**: `ollama pull qwen2.5:32b-instruct-q4_K_M` (or set `OLLAMA_MODEL` in `.env` to a model you've already pulled, e.g. `qwen2.5:14b-instruct` for faster but lower-quality results)

**Summarization/translation slow:**
- **Symptom**: Each article takes 10-30+ seconds
- **Cause**: A 32B model is compute-heavy; the first call after Ollama has been idle also pays a one-time model-load cost
- **Solution**: Switch `OLLAMA_MODEL` to a smaller Qwen2.5 variant (7B/14B) if throughput matters more than summary quality; test with `--max N` first

### X/Twitter Collection

**0 posts collected:**
- **Symptom**: `collect_x.py` reports 0 posts
- **Cause**: Public Nitter instances are frequently rate-limited or down — this is expected, not a bug
- **Solution**: Re-run later, or set `NITTER_INSTANCES` in `.env` to an instance that's currently up

### Funding Section (OpenAI, optional)

**OpenAI quota error:**
- **Symptom**: `insufficient_quota` error
- **Solution**: Add billing credits at https://platform.openai.com/billing
- **Symptom**: Rate limit 429
- **Solution**: Script retries with backoff (20s, 40s, 60s delays)

**No `OPENAI_API_KEY` set:**
- **Behavior**: The web-search supplement is skipped; the funding table still shows events extracted locally via Ollama from collected posts

## Success Metrics

After each run, verify:
1. **Coverage**: 50-150 posts collected (single-source Twitter/X; 0 is possible if Nitter is down — re-run)
2. **Summary Quality**: Manually review 10 random summaries
   - Target: Clear, concise, captures key points — expect slightly lower fidelity than Claude, especially on messy tweet text
3. **Runtime**: Complete workflow in a few minutes once Ollama is warm
4. **Cost**: $0.00-0.10 per run (only the optional OpenAI funding search costs anything)
5. **Data Quality**: No duplicate posts, retweets excluded

## Troubleshooting

### No posts collected
```bash
python tools/collect_x.py --start_date 2026-07-06 --end_date 2026-07-20

# Verify .tmp/ files created and contain data
ls -lh .tmp/
cat .tmp/raw_x.json | jq length
```

### Summarization fails
```bash
# Test Ollama is reachable and the model is pulled
curl -s http://localhost:11434/api/chat -d '{"model":"qwen2.5:32b-instruct-q4_K_M","messages":[{"role":"user","content":"hi"}],"stream":false}'

# Test with smaller batch
python tools/summarize_articles.py --max 5
```

### Word document generation fails
```bash
# Check if python-docx is installed
python -c "import docx; print(docx.__version__)"

# Manually inspect intermediate files
cat .tmp/summarized_articles.json | jq '.[0]'
```

## Lessons Learned

### 2026-07-20 Update (current)
- **Removed Claude/Anthropic entirely.** All summarization, translation, executive-summary, and funding-extraction calls that used to hit `api.anthropic.com` now go through a local **Ollama** model instead (`qwen2.5:32b-instruct-q4_K_M` by default, configurable via `OLLAMA_MODEL`/`OLLAMA_HOST` in `.env`). This eliminates all per-run LLM cost for these steps — only the optional OpenAI funding web search still costs anything.
- **Twitter/X is now the only collection source.** TechCrunch (`collect_techcrunch.py`) and TLDR (`collect_tldr.py`) are no longer part of this workflow — dropped in favor of `collect_x.py` alone. This also removes the `NEWSAPI_ORG_KEY` requirement and the Gmail OAuth setup (`credentials.json`/`token.json`) that TLDR collection needed. Those collector scripts still exist in `tools/` in case this needs to be reverted, they're just unused by this SOP now.
- Expect a quality tradeoff vs. Claude, especially on messy/short tweet text and JSON-extraction edge cases — the fallback behavior (using article description on parse failure) is unchanged, it just triggers a bit more often.
- Ollama's first call after being idle pays a one-time model-load cost (~10s observed for the 32B q4 model); warm calls are ~2s. No code changes needed for this — just expect the first article in a run to be slower.

### 2026-07-07 Update
- **X/Twitter added as a source** via `collect_x.py`. X has no free/reliable API and blocks scraping, so collection goes through **Nitter RSS** (free, no key).
  - Follow list lives in `x_accounts.txt` (repo root): `handle` lines for account timelines, `search: QUERY` lines for tweet searches. Curated defaults cover AI labs, founders/researchers, and VCs/investors.
  - Account timelines are the reliable path. **Nitter search is frequently disabled** on public instances, so the `search:` (trending/funding) lines are best-effort and often return nothing.
  - Public Nitter instances rot constantly. The collector tries several in order (`nitter.net`, `nitter.poast.org`, `nitter.privacydev.net`, `lightbrd.com`) and reuses the first that works. Override with `NITTER_INSTANCES` in `.env` when defaults die. **A run returning 0 posts is expected when instances are down — not a bug.**
  - Full tweet text is captured into `content`/`description` at collection time. `summarize_articles.py` was updated so X posts **with captured text are summarized/scored normally** (fetch is skipped, since x.com can't be scraped); only X links with no captured text fall back to the old description-only behavior. This means tweets now get category/relevance/vc_signal scores and respect `--min-signal`.
  - Retweets (Nitter `RT by @...`) are skipped; replies are kept (down-ranked by relevance scoring).

### 2026-06-06 Update
- **VC signal scoring**: `summarize_articles.py` now scores each article 1-5 on VC investment value (not just AI practitioner value) and classifies a `vc_signal` type: `funding`, `product`, `partnership`, `hire`, `regulatory`, `research`, `other`
  - Scores appear as the 优先级 column in the Word doc
  - Signal type appears as a colored badge next to the article title (e.g., `[融资]` in blue)
  - Use `--min-signal 3` in generate_word_doc.py to drop low-value articles and cut noise
- **Watchlist tracking**: Add company names to `watchlist.txt` (one per line) to get a "Watchlist Highlights" section at the top of every report. Any article mentioning a tracked company is surfaced there automatically.
- **Funding dedup**: `_merge_funding_events()` in `generate_word_doc.py` merges article-extracted and web-search-extracted events by company name, keeping the entry with the most complete data.

### 2026-02-25 Update
- Added **Chinese translation** via LLM (`--translate` flag in generate_word_doc.py)
  - Translates each article summary to Simplified Chinese
  - Chinese appears first in the cell, English below
- Added **AI Fundraising News section** at end of Word doc
  - Uses **ChatGPT `gpt-4o-search-preview`** with live web search — not limited to collected articles
  - Requires paid OpenAI account with credits (`insufficient_quota` = needs billing at platform.openai.com/billing)
  - Returns JSON array; if model returns prose (no events found), section shows "No AI funding events found"
- Word doc format updated:
  - 2-column table (Date | Title+Summary) — no more 3-column layout
  - Bullet points converted to flowing paragraphs
  - Markdown `**bold**` markers render as actual Word bold
- Deduplication now done inline (no dedicated script needed)

### 2026-01-25 Update
- Removed translation step (no longer needed at the time)
- Added article summarization with full content fetching

### Initial Implementation (2026-01-22)
- System successfully implemented and tested

---

## Quick Reference

**Run full workflow:**
1. Ensure Ollama is running (`ollama serve` or the Ollama app) with `qwen2.5:32b-instruct-q4_K_M` pulled
2. (Optional) Ensure `.env` has `OPENAI_API_KEY` for the funding web-search supplement
3. Execute pipeline:
   ```bash
   START_DATE="2026-07-06"
   END_DATE="2026-07-20"

   # Phase 2: Collection
   python tools/collect_x.py --start_date $START_DATE --end_date $END_DATE

   # Phase 3: Deduplication (inline)
   python3 -c "
   import json
   x = json.load(open('.tmp/raw_x.json'))
   seen, unique = set(), []
   for a in x:
       url = a.get('url', '')
       if url and url not in seen:
           seen.add(url); unique.append(a)
   json.dump(unique, open('.tmp/classified_articles.json', 'w'), ensure_ascii=False, indent=2)
   print(f'Deduped: {len(unique)} posts')
   "

   # Phase 4: Summarization with local Ollama
   python tools/summarize_articles.py --provider ollama --yes

   # Phase 5: Word doc with Chinese translation + funding section
   # Add --min-signal 3 to filter low-signal articles; edit watchlist.txt to track target companies
   python tools/generate_word_doc.py --start_date $START_DATE --end_date $END_DATE \
     --articles .tmp/summarized_articles.json --translate --min-signal 3
   ```

4. Find output: `output/AI_News_YYYYMMDD_YYYYMMDD.docx`

**Typical cost per run:**
- Collection + summarization + translation + funding extraction (Ollama): $0.00
- Funding web-search supplement (OpenAI, optional): ~$0.05-0.10
- **Total**: ~$0.00-0.10
