# Collect AI News (Bi-Weekly)

## Objective
Collect AI-related news from X/Twitter (the only source), summarize and translate to Chinese using Claude, and output a formatted Word document with two sections: (1) AI News table and (2) AI Fundraising News table sourced from a live Claude web search.

## Required Inputs
- **Start date** (YYYY-MM-DD)
- **End date** (YYYY-MM-DD)
- **API keys** in `.env`:
  - `ANTHROPIC_API_KEY` — **required**. Every inference in this workflow runs on Claude: article summarization, relevance scoring, Chinese translation, executive summary, funding extraction from collected posts, and the funding web search.
- **Optional Claude overrides** in `.env` (all `NEWS_`-prefixed so they can't collide with the `CLAUDE_*` vars a Claude Code shell already sets):
  - `NEWS_CLAUDE_MODEL` — default `claude-opus-5`; set to `claude-sonnet-5` or `claude-haiku-4-5` to trade quality for cost on large runs
  - `NEWS_CLAUDE_EFFORT` — default `low` (thinking depth for the per-article calls); raise to `medium`/`high` if summaries read thin
  - `NEWS_CLAUDE_MAX_TOKENS` — default `4096`; the floor applied to every call's token budget
- **X/Twitter source** (the primary and only automated source):
  - `x_accounts.txt` - handles + search queries to follow (repo root, editable)
  - Account timelines come from free Nitter RSS mirrors (no key). When a mirror won't serve an account, that account falls back to Claude web search — so a Nitter outage degrades the report instead of emptying it
  - `NITTER_INSTANCES` in `.env` (optional) - comma-separated Nitter instances to override the built-in defaults when they are down

## Tools Required
1. `tools/collect_x.py` - X/Twitter posts via Nitter RSS, with a Claude web-search fallback (accounts + searches from `x_accounts.txt`)
2. `tools/collect_x_claude.py` - the fallback itself; also runnable standalone to collect without Nitter at all
3. `tools/summarize_articles.py` - Fetch full content and generate summaries via Claude
4. `tools/generate_word_doc.py` - Word document generation with translation and funding section
5. `tools/utils.py` - Shared utilities (imported by other tools; also hosts the `call_claude` / `translate_to_chinese_claude` / `get_claude_client` helpers)

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
   - **Two paths, Nitter first.** Account timelines come from free Nitter RSS (real tweet text, no key). Any account no instance serves is retried through Claude web search, and the `search:` topic queries always go through Claude — public Nitter instances disabled tweet search, so those lines used to return nothing on every run
   - Full tweet text is captured into `content`/`description` so the summarizer can score it without scraping x.com (which is blocked)
   - Retweets are skipped; posts are filtered to the date range
   - **Read the health line.** Every run prints `Nitter health: N/M accounts served, X unserved, Y empty feed(s)` and names the unserved accounts, so a low yield is explainable instead of silent
   - Fallback items are labelled `X/Twitter (via Claude search)` and carry `via_claude_search: true`. They are **coverage of what an account announced, not the tweet itself** — x.com blocks automated readers, so the URL points at the source that verifies the claim
   - Expected: 50-150 posts per 2-week range. The step **exits non-zero on 0 posts**, which stops the pipeline at the real cause rather than producing an empty document three phases later

   **Flags:**
   - `--fallback auto` (default) — Claude fallback only when the run looks degraded (an account went unserved, or the whole run came back under `--min-posts`)
   - `--fallback always` — chase every account Nitter produced nothing for, even on a healthy run. Widest coverage, highest cost
   - `--fallback never` — Nitter only, zero API spend in this step; accept that an outage means an empty report
   - `--min-posts N` (default 20) — the "degraded" threshold for `auto`. `--min-posts 0` means only unserved accounts trigger the fallback

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
   python tools/summarize_articles.py --provider claude --yes
   ```
   - Input: `.tmp/classified_articles.json`
   - Uses **Claude** (`claude-opus-5` by default, effort `low`) to generate concise paragraph summaries, category, relevance score, and VC signal type
   - X posts already have full tweet text captured at collection time, so no fetch is needed — the tweet text is summarized/scored directly
   - Output: `.tmp/summarized_articles.json`
   - **Accuracy controls**: summary length scales to source length; prompts forbid outside knowledge and unsourced figures; every figure is arithmetically checked against the source, with one targeted repair pass when a mismatch is found. Watch for the `⚠ GROUNDING` block at the end of the step — anything listed there is an article to eyeball before sending
   - Cost: roughly $0.03 per post at Opus 5 list rates — the script prints an estimate and asks for confirmation above $2 unless `--yes` is passed
   - Estimated time: ~5-15s per post (network-bound, one request each); progress is saved after every article, so a re-run resumes rather than repeats

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
     - **Primary source**: Claude extracts structured funding events from collected posts
     - **Supplemental source**: Claude with the server-side `web_search` tool fills gaps, one search turn per day in the range (skipped entirely if `ANTHROPIC_API_KEY` is unset)
     - Two sources are merged and deduplicated by company name (richer entry wins)
   - Output: `output/AI_News_20260706_20260720.docx`
   - Estimated cost: a few dollars per run — the funding web search is the expensive part (one multi-search turn per day in the range, plus $10 per 1,000 searches)

   **Flags:**
   - `--min-signal N` — only include articles with relevance ≥ N (1=all, 3=curated, 4=high-signal only)
   - `--watchlist FILE` — path to watchlist file (default: `watchlist.txt`)

## Expected Outputs

**Primary Deliverable:**
- Word document: `output/AI_News_[start]_[end].docx`
  - **AI News Summary** table: all posts grouped by category, 3 columns (Date | 优先级 | Chinese summary with hyperlinked title)
  - **AI Fundraising News** table: funding events extracted from collected posts + supplemented via Claude web search

**Intermediate Files (in `.tmp/`):**
- `raw_x.json` - X/Twitter posts collected via Nitter RSS (source `X/Twitter`)
- `classified_articles.json` - Deduplicated posts
- `summarized_articles.json` - Posts with Claude-generated summaries

## Edge Cases & Error Handling

### Claude

**Missing key:**
- **Symptom**: `ERROR: ANTHROPIC_API_KEY not found in .env file`, or `WARNING: ANTHROPIC_API_KEY not set in .env` followed by empty summaries
- **Solution**: Add `ANTHROPIC_API_KEY` to `.env` (https://console.anthropic.com/settings/keys)

**SDK missing:**
- **Symptom**: `WARNING: anthropic package not installed`
- **Solution**: `pip install -r requirements.txt` (or `pip install anthropic`)

**Rate limits:**
- **Symptom**: run slows down or a few articles fall back to their raw description
- **Cause**: 429s from the API. The SDK retries automatically with backoff (5 attempts) before a call gives up and returns `''`
- **Solution**: Re-run the same command — already-summarized articles are skipped, so only the failures are retried

**Truncated output:**
- **Symptom**: `WARNING: Claude hit max_tokens (...) — output may be truncated`
- **Cause**: thinking and response text share the token budget on Opus 5
- **Solution**: raise `NEWS_CLAUDE_MAX_TOKENS` in `.env` (default 4096)

**Run costs more than expected:**
- **Solution**: set `NEWS_CLAUDE_MODEL=claude-sonnet-5` (or `claude-haiku-4-5`) in `.env`, and/or `--min-signal 3` to cut the article count before doc generation

### X/Twitter Collection

**0 posts collected:**
- **Symptom**: `collect_x.py` reports 0 posts and exits non-zero
- **Cause**: Both paths failed — no Nitter instance served anything AND the Claude fallback found nothing (usually a missing `ANTHROPIC_API_KEY`, since the fallback needs it)
- **Solution**: Check the key, then try `--fallback always`, a wider date range, or set `NITTER_INSTANCES` in `.env`

**Fewer posts than usual / lots of `[skip]` lines:**
- **Symptom**: the health line shows several unserved accounts
- **Cause**: Nitter mirrors rotting or rate-limiting — the normal failure mode
- **Solution**: Nothing required; `--fallback auto` already covered those accounts via Claude. To verify, check how many items in `.tmp/raw_x.json` carry `via_claude_search: true`. For a permanent fix, put a working mirror in `NITTER_INSTANCES`

**Everything came from Claude search (`0 via Nitter`):**
- **Cause**: every mirror in the list is down — this is the outage the fallback exists for
- **Impact**: the report is built from coverage of announcements rather than tweet text, so summaries read more like news articles. Content and dates are still sourced and verified
- **Solution**: refresh `NITTER_INSTANCES` when you get a chance; the pipeline keeps working meanwhile

### Funding Section (Claude web search)

**Search turn ends early:**
- **Symptom**: a day returns 0 events even though news exists
- **Cause**: the model paused mid-search (`pause_turn`). The tool resumes automatically up to 3 times, then gives up for that day
- **Solution**: re-run for the affected date range

**No `ANTHROPIC_API_KEY` set:**
- **Behavior**: The web-search supplement is skipped; the funding table still shows events Claude extracted from collected posts (which also needs the key, so with no key at all the section is skipped entirely)

## Success Metrics

After each run, verify:
1. **Coverage**: 50-150 posts collected (single-source Twitter/X; 0 is possible if Nitter is down — re-run)
2. **Summary Quality**: Manually review 10 random summaries
   - Target: Clear, concise, captures key points, no invented entities or figures
3. **Runtime**: Collection is the fast part; summarization is one API call per post
4. **Cost**: single-digit dollars per run — dominated by per-article summarization and the day-by-day funding web search
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
# Test the key and SDK are working
python -c "from dotenv import load_dotenv; load_dotenv(override=True); \
from tools.utils import call_claude; print(call_claude('Reply with exactly: OK', max_tokens=64))"

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

### 2026-08-12 Update — factual accuracy (current)
- **Summary length now scales to the source** (`_length_rule` in `summarize_articles.py`). The prompt used to demand 4-6 sentences from every article including a 200-character tweet, which is an instruction to invent four sentences of significance. Measured on 10 real short posts, summaries went from ~320 characters to ~130 — the old ones were *longer than their English sources*, i.e. the model was adding material. One old summary fabricated a competitor chip announcement, product codename included; the new summary on the same source contains only what the source says.
- **Every summarization and extraction prompt carries explicit grounding rules** (`GROUNDING_RULES_ZH` / `_EN`): no outside knowledge, figures and names must match the source, write less when the source is thin, omit rather than hedge, and never write sentences about what the source failed to mention.
- **Figures are checked arithmetically, not by asking the model** (`tools/grounding.py`, 10 self-tests — run `python tools/grounding.py`). Every number in a summary is normalized to a value and matched against the source, so `$50 million` → `5000万美元` is recognized as correct while an invented `6000万美元` is caught. Only money, percentages and large counts are checked; small counts and bare years are skipped because sources spell them as words.
- **One targeted repair, not a self-review pass.** When the arithmetic check finds a mismatch, the model is told exactly which figures are unsupported and asked to remove or rewrite only those. Verified: a summary with one true figure and two invented ones came back with the true figure intact and both inventions gone. A general "double-check your work" pass was deliberately not used — it costs a call per article and tends to ratify.
- **Anything that survives repair is flagged, not hidden.** The article gets `grounding_flags` in the output JSON and the run prints a summary; a clean run prints `✓ Grounding: every figure in every summary traces to its source`.
- **Translation preserves proper nouns.** An earlier run rendered ByteDance as "BiteDance"; the translation prompt now pins company/product/person names to their original spelling and forbids adding or dropping content.
- **The verification service was never actually receiving claims.** `requirements.txt` pointed at `../verification-service/envelope`; the real path is `../INCE-Verification-Service/envelope`, so the editable install silently no-op'd and `emit_funding_claims()` logged a warning and submitted nothing. Fixed and installed — funding claims now reach the service for independent re-corroboration.

### 2026-08-12 Update — X reliability
- **X/Twitter is the primary source, and it no longer has a single point of failure.** Nitter RSS is still tried first (it returns the real tweet text), but any account no instance serves now falls back to `tools/collect_x_claude.py`, which asks Claude — with server-side web search — what that account announced in the window. Verified by pointing `NITTER_INSTANCES` at a dead host: a total blackout that used to produce 0 posts produced 8.
- **The `search:` topic lines work for the first time.** Public Nitter instances disabled tweet search, so those lines had been silently returning nothing on every run. They now route through Claude web search and are filtered at `strict`.
- **Nitter failures are visible instead of silent.** Each run prints `Nitter health: N/M accounts served, X unserved, Y empty` and names the unserved accounts. Previously "0 posts in range" meant either a quiet account or a broken mirror, with no way to tell.
- **0 posts is now a hard failure** (non-zero exit) rather than an empty document generated three phases later.
- **Fallback items are labelled, not laundered.** Source is `X/Twitter (via Claude search)` and `via_claude_search: true` — they are coverage of an announcement, not the tweet, because x.com blocks automated readers. They go through the same promo/AI/news-signal filters as Nitter posts, at the strictest level among the accounts requested.
- **Search results are schema-constrained** (`output_config.format`), so a drift in how the model formats output can't break parsing. The same change removed the regex-scraping step from the funding search and deal sourcing.

### 2026-08-12 Update — Claude migration
- **Everything runs on Claude again.** Ollama is gone from the pipeline: `call_ollama` → `call_claude` in `tools/utils.py`, and every call site (summarization, categorization, funding extraction, translation, executive summary, insights, debate agents, RootData deal intros) now goes through the Anthropic API. `ANTHROPIC_API_KEY` is the only key this workflow needs.
- **The funding web search moved off OpenAI too.** `gpt-4o-search-preview` was replaced by Claude with the server-side `web_search` tool (`extract_funding_with_web_search` in `generate_word_doc.py`, and the same change in `source_deals.py`). One fewer provider, one fewer key, and results now come back with source URLs attached.
- **Cost is back, and it is per-article.** Roughly $0.03/post for summarization at Opus 5 list rates plus the day-by-day funding search. `summarize_articles.py` prints an estimate and prompts above $2 unless `--yes` is passed. Set `NEWS_CLAUDE_MODEL=claude-sonnet-5` for a cheaper run.
- **Env vars are `NEWS_`-prefixed on purpose.** A bare `CLAUDE_EFFORT` is already set inside a Claude Code shell; an unprefixed name would let the pipeline silently inherit it and quietly change cost and behavior when run from there.
- **The consumer-mode self-critique second pass is now opt-in** (`CONSUMER_REFINE=1`). It existed to close the gap between a small local model and a hosted one; on Claude it mostly bought over-verification at double the per-article cost.
- **Thinking shares the token budget.** On Opus 5, `max_tokens` caps thinking *and* answer together, which is why `call_claude` floors every request at `NEWS_CLAUDE_MAX_TOKENS` (4096) rather than passing through the small budgets the old `num_predict` call sites used.

### 2026-07-20 Update
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
1. Ensure `.env` has `ANTHROPIC_API_KEY` and `pip install -r requirements.txt` has been run
2. (Optional) Set `NEWS_CLAUDE_MODEL` in `.env` to run the batch on a cheaper model
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

   # Phase 4: Summarization with Claude
   python tools/summarize_articles.py --provider claude --yes

   # Phase 5: Word doc with Chinese translation + funding section
   # Add --min-signal 3 to filter low-signal articles; edit watchlist.txt to track target companies
   python tools/generate_word_doc.py --start_date $START_DATE --end_date $END_DATE \
     --articles .tmp/summarized_articles.json --translate --min-signal 3
   ```

4. Find output: `output/AI_News_YYYYMMDD_YYYYMMDD.docx`

**Typical cost per run (Opus 5 list rates; collection itself is free):**
- Summarization + translation + funding extraction: ~$0.03 per post
- Funding web-search supplement: one multi-search turn per day in the range, plus $10 per 1,000 searches
- **Total**: single-digit dollars for a typical bi-weekly run — cut it by setting `NEWS_CLAUDE_MODEL=claude-sonnet-5` or filtering with `--min-signal 3`
