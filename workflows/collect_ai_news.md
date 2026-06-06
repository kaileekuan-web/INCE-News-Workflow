# Collect AI News (Bi-Weekly)

## Objective
Collect AI-related news from multiple sources (TechCrunch, TLDR AI, TLDR Main), summarize with Claude, translate summaries to Chinese, and output a formatted Word document with two sections: (1) AI News table and (2) AI Fundraising News table sourced from a live ChatGPT web search.

## Required Inputs
- **Start date** (YYYY-MM-DD)
- **End date** (YYYY-MM-DD)
- **API keys** in `.env`:
  - `ANTHROPIC_API_KEY` - for article summarization (Claude) and Chinese translation
  - `OPENAI_API_KEY` - for AI funding news search (ChatGPT with web search)
  - `NEWSAPI_ORG_KEY` - for TechCrunch
- **Gmail OAuth** setup for TLDR newsletters:
  - `credentials.json` - downloaded from Google Cloud Console
  - `token.json` - auto-generated on first run

## Tools Required
1. `tools/collect_techcrunch.py` - TechCrunch via NewsAPI.org
2. `tools/collect_tldr.py` - TLDR newsletters via Gmail API (with section filtering)
3. `tools/summarize_articles.py` - Fetch full content and generate summaries via Claude
4. `tools/generate_word_doc.py` - Word document generation with translation and funding section
5. `tools/utils.py` - Shared utilities (imported by other tools)

## Steps

### Phase 1: User Input
1. **Prompt user for date range:**
   ```
   Enter start date (YYYY-MM-DD): 2026-01-08
   Enter end date (YYYY-MM-DD): 2026-01-24
   ```

2. **Validate dates** using `utils.validate_date_range()`
   - Must be in YYYY-MM-DD format
   - Start date must be before end date
   - Range cannot exceed 1 year

### Phase 2: Data Collection (Run in Parallel)

3. **Collect TechCrunch articles:**
   ```bash
   python tools/collect_techcrunch.py --start_date 2026-01-08 --end_date 2026-01-24
   ```
   - Output: `.tmp/raw_techcrunch.json`
   - Uses NewsAPI.org (100 req/day free tier)
   - Expected: 5-15 articles for 2-week range

4. **Collect TLDR newsletters:**
   ```bash
   python tools/collect_tldr.py --start_date 2026-01-08 --end_date 2026-01-24
   ```
   - Output: `.tmp/raw_tldr_ai.json`, `.tmp/raw_tldr_main.json`
   - Uses Gmail API to search inbox
   - **Section Filtering**:
     - TLDR AI: Only `"headlines & launches"` section
     - TLDR Main: Only `"big tech & startups"` and `"miscellaneous"` sections
   - **Promotional Content Filtering**: Removes ads like "Get a demo", "View online", "Apply here", etc.
   - First run requires OAuth browser authentication
   - Expected: 20-35 AI articles, 40-70 Main articles per 2-week range

### Phase 3: Deduplication

5. **Deduplicate articles** (inline Python — no dedicated script):
   ```python
   import json

   with open('.tmp/raw_tldr_ai.json') as f: tldr_ai = json.load(f)
   with open('.tmp/raw_tldr_main.json') as f: tldr_main = json.load(f)
   with open('.tmp/raw_techcrunch.json') as f: techcrunch = json.load(f)

   all_articles = tldr_ai + tldr_main + techcrunch
   seen, unique = set(), []
   for a in all_articles:
       url = a.get('url', '')
       if url and url not in seen:
           seen.add(url)
           unique.append(a)

   with open('.tmp/classified_articles.json', 'w') as f:
       json.dump(unique, f, ensure_ascii=False, indent=2)
   ```
   - Output: `.tmp/classified_articles.json`
   - Expected: 60-100 unique articles per 2-week range

### Phase 4: Article Summarization

6. **Fetch and summarize articles:**
   ```bash
   python tools/summarize_articles.py --provider claude --yes
   ```
   - Input: `.tmp/classified_articles.json`
   - Uses **Claude** (`claude-sonnet-4-20250514`) to generate concise paragraph summaries
   - For each article: fetches full content, then summarizes
   - X/Twitter links are skipped automatically (unscrappable)
   - Paywall articles (WSJ, Bloomberg, NYT) fall back to article description
   - Output: `.tmp/summarized_articles.json`
   - Estimated cost: ~$0.50-1.00 per 100 articles
   - Estimated time: 3-6 minutes for 100 articles
   - `--yes` flag skips the cost confirmation prompt

### Phase 5: Word Document Generation

7. **Generate Word document with Chinese translation and funding section:**
   ```bash
   python tools/generate_word_doc.py --start_date 2026-01-08 --end_date 2026-01-24 \
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
   - **Section 2 — AI Fundraising News table** (7 columns):
     - Columns: Date | Company | Summary | Stage | Raise | Valuation | Investors
     - **Primary source**: Claude extracts structured funding events from collected articles
     - **Supplemental source**: ChatGPT (`gpt-4o-search-preview`) live web search fills gaps
     - Two sources are merged and deduplicated by company name (richer entry wins)
     - Uses `ANTHROPIC_API_KEY` (primary) + `OPENAI_API_KEY` (supplement)
   - Output: `output/AI_News_20260108_20260124.docx`
   - Estimated time: 5-10 minutes (translation is the bottleneck at ~3s/article)
   - Estimated cost: ~$0.50-1.00 Claude translation + ~$0.02 Claude funding extraction + ~$0.05-0.10 OpenAI funding search

   **New flags:**
   - `--min-signal N` — only include articles with relevance ≥ N (1=all, 3=curated, 4=high-signal only)
   - `--watchlist FILE` — path to watchlist file (default: `watchlist.txt`)

## Expected Outputs

**Primary Deliverable:**
- Word document: `output/AI_News_[start]_[end].docx`
  - **AI News Summary** table: all articles sorted oldest → newest, 2 columns (Date | Chinese+English summary with hyperlinked title)
  - **AI Fundraising News** table: funding events found via ChatGPT web search, 6 columns (Date | Company | Summary | Stage | Raise | Investors)

**Intermediate Files (in `.tmp/`):**
- `raw_techcrunch.json` - TechCrunch articles
- `raw_tldr_ai.json` - TLDR AI newsletter items (filtered to "Headlines & Launches" only)
- `raw_tldr_main.json` - TLDR Main newsletter items ("Big Tech & Startups" + "Miscellaneous" only)
- `classified_articles.json` - Deduplicated articles combined from all sources
- `summarized_articles.json` - Articles with Claude-generated summaries

## Edge Cases & Error Handling

### API Rate Limits

**NewsAPI.org (100 requests/day):**
- **Symptom**: Error 429 or "rate limit exceeded"
- **Solution**: Script automatically falls back to TechCrunch RSS feed
- **Prevention**: Run once per day maximum

**Claude API (summarization + translation):**
- **Symptom**: Read timeout or 429
- **Solution**: Script retries automatically; occasional timeouts are normal and those articles fall back to description
- **Prevention**: Use `--max N` to test with fewer articles first

**OpenAI API (funding section):**
- **Symptom**: `insufficient_quota` error
- **Solution**: Add billing credits at https://platform.openai.com/billing
- **Symptom**: Rate limit 429
- **Solution**: Script retries with backoff (20s, 40s, 60s delays)

**Gmail API:**
- **Symptom**: 403 forbidden or quota exceeded
- **Solution**: Check Google Cloud Console quotas
- **Note**: 1 billion quota units/day (effectively unlimited for this use case)

### Authentication Failures

**Gmail OAuth expired:**
- **Symptom**: `token.json` invalid or expired
- **Solution**:
  ```bash
  rm token.json
  python tools/collect_tldr.py --start_date ... --end_date ...
  # Browser will open for re-authentication
  ```

**Missing API keys:**
- **Symptom**: "ERROR: [KEY] not found in .env"
- **Solution**:
  1. Copy `.env.example` to `.env`
  2. Add your API keys
  3. For NewsAPI.org: https://newsapi.org/
  4. For OpenAI: https://platform.openai.com/api-keys

### Content Fetching Issues

**Article fetching fails:**
- **Symptom**: "Could not fetch [URL]" warnings
- **Causes**: Paywall, anti-scraping, timeout
- **Fallback**: Uses article description/title for summary
- **Note**: Some failures are expected (10-20%)

**Content extraction incomplete:**
- **Issue**: Some websites have complex layouts
- **Behavior**: Script extracts what it can, GPT generates summary from available text
- **Workaround**: Most article descriptions are sufficient for meaningful summaries

### Missing Data

**TLDR emails not found:**
- **Check**: Date range is correct (TLDR sends daily)
- **Check**: Emails not in spam folder
- **Check**: Subscription is active
- **Workaround**: Script continues with empty list if no emails found

**TLDR section parsing fails:**
- **Symptom**: Fewer items than expected
- **Behavior**: Falls back to extracting all links if sections not found
- **Solution**: Check actual email HTML structure, may need to update section names in `collect_tldr.py`

**Z Potentials URL changed:**
- **Symptom**: RSS feed returns 0 entries
- **Solution**: Update `Z_POTENTIALS_URL` in `.env`

### Summarization Quality

**Summaries too generic:**
- **Issue**: Content fetch failed or article too short
- **Behavior**: GPT uses title + description only
- **Note**: Usually still produces usable summary

**Rate limiting during summarization:**
- **Symptom**: Script slows down or fails midway
- **Solution**: Script has built-in delays (0.3s per article)
- **Recovery**: Summarized articles saved incrementally (if implemented) or re-run with `--max` to process in batches

## Success Metrics

After each run, verify:
1. **Coverage**: 200+ articles collected from all sources combined
2. **Summary Quality**: Manually review 10 random summaries
   - Target: Clear, concise, captures key points
3. **Runtime**: Complete workflow in < 10 minutes
4. **Cost**: < $1.50 per run
5. **Data Quality**: No duplicate articles, no promotional content

## Troubleshooting

### No articles collected
```bash
# Check each source individually
python tools/collect_techcrunch.py --start_date 2026-01-08 --end_date 2026-01-24
python tools/collect_tldr.py --start_date 2026-01-08 --end_date 2026-01-24
python tools/collect_substack.py --start_date 2026-01-08 --end_date 2026-01-24

# Verify .tmp/ files created and contain data
ls -lh .tmp/
cat .tmp/raw_techcrunch.json | jq length
```

### Summarization fails
```bash
# Test OpenAI API key
python -c "from openai import OpenAI; import os; from dotenv import load_dotenv; load_dotenv(); client = OpenAI(api_key=os.getenv('OPENAI_API_KEY')); print(client.models.list())"

# Check .env file
cat .env | grep OPENAI_API_KEY

# Test with smaller batch
python tools/summarize_articles.py --max 5
```

### Gmail authentication issues
```bash
# Remove existing token and re-authenticate
rm token.json
python tools/collect_tldr.py --start_date 2026-01-08 --end_date 2026-01-24
```

### Word document generation fails
```bash
# Check if python-docx is installed
python -c "import docx; print(docx.__version__)"

# Manually inspect intermediate files
cat .tmp/summarized_articles.json | jq '.[0]'
```

### TLDR collecting promotional content
```bash
# Check the PROMOTIONAL_FILTERS list in collect_tldr.py
# Add more filter terms if needed
```

### TLDR section filtering not working
```bash
# Verify section names in email HTML
# Update TLDR_AI_SECTIONS and TLDR_MAIN_SECTIONS in collect_tldr.py
```

## Lessons Learned

### 2026-06-06 Update (current)
- **VC signal scoring**: `summarize_articles.py` now scores each article 1-5 on VC investment value (not just AI practitioner value) and classifies a `vc_signal` type: `funding`, `product`, `partnership`, `hire`, `regulatory`, `research`, `other`
  - Scores appear as the 优先级 column in the Word doc
  - Signal type appears as a colored badge next to the article title (e.g., `[融资]` in blue)
  - Use `--min-signal 3` in generate_word_doc.py to drop low-value articles and cut noise
- **Watchlist tracking**: Add company names to `watchlist.txt` (one per line) to get a "Watchlist Highlights" section at the top of every report. Any article mentioning a tracked company is surfaced there automatically.
- **Claude-based funding extraction**: `detect_funding.py` now has `extract_funding_with_claude()` which uses Claude Haiku (~$0.02 for a full run) to extract structured funding events from collected articles — far more accurate than the previous regex approach. This runs first in `generate_word_doc.py`; OpenAI web search then supplements any gaps.
- **Funding dedup**: `_merge_funding_events()` in `generate_word_doc.py` merges article-extracted and web-search-extracted events by company name, keeping the entry with the most complete data.

### 2026-02-25 Update
- Switched summarization from OpenAI to **Claude** (`claude-sonnet-4-20250514`) — more reliable, better quality
- Added **Chinese translation** via Claude (`--translate` flag in generate_word_doc.py)
  - Translates each article summary to Simplified Chinese
  - Chinese appears first in the cell, English below
  - Occasional read timeouts are normal; those articles show English only
- Added **AI Fundraising News section** at end of Word doc
  - Uses **ChatGPT `gpt-4o-search-preview`** with live web search — not limited to collected articles
  - Requires paid OpenAI account with credits (`insufficient_quota` = needs billing at platform.openai.com/billing)
  - Returns JSON array; if model returns prose (no events found), section shows "No AI funding events found"
- Word doc format updated:
  - 2-column table (Date | Title+Summary) — no more 3-column layout
  - Bullet points converted to flowing paragraphs
  - Articles sorted oldest → newest
  - Markdown `**bold**` markers render as actual Word bold
- Deduplication now done inline (no dedicated script needed)
- TLDR section filtering tightened:
  - TLDR AI: `"headlines & launches"` only
  - TLDR Main: `"big tech & startups"` and `"miscellaneous"` only
- Substack (`collect_substack.py`) no longer used in standard workflow

### 2026-01-25 Update
- Removed translation step (no longer needed at the time)
- Added article summarization with full content fetching
- Updated TLDR collector to filter specific sections and remove promotional content

### Initial Implementation (2026-01-22)
- System successfully implemented and tested
- Gmail OAuth setup requires one-time manual authentication

---

## Quick Reference

**Run full workflow:**
1. Ensure `.env` has `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `NEWSAPI_ORG_KEY`
2. Ensure `credentials.json` exists (for Gmail)
3. Execute pipeline:
   ```bash
   START_DATE="2026-03-02"
   END_DATE="2026-03-13"

   # Phase 2: Collection (run in parallel)
   python tools/collect_techcrunch.py --start_date $START_DATE --end_date $END_DATE &
   python tools/collect_tldr.py --start_date $START_DATE --end_date $END_DATE &
   wait

   # Phase 3: Deduplication (inline)
   python3 -c "
   import json
   ai = json.load(open('.tmp/raw_tldr_ai.json'))
   main = json.load(open('.tmp/raw_tldr_main.json'))
   tc = json.load(open('.tmp/raw_techcrunch.json'))
   seen, unique = set(), []
   for a in ai + main + tc:
       url = a.get('url', '')
       if url and url not in seen:
           seen.add(url); unique.append(a)
   json.dump(unique, open('.tmp/classified_articles.json', 'w'), ensure_ascii=False, indent=2)
   print(f'Deduped: {len(unique)} articles')
   "

   # Phase 4: Summarization with Claude
   python tools/summarize_articles.py --provider claude --yes

   # Phase 5: Word doc with Chinese translation + funding section
   # Add --min-signal 3 to filter low-signal articles; edit watchlist.txt to track target companies
   python tools/generate_word_doc.py --start_date $START_DATE --end_date $END_DATE \
     --articles .tmp/summarized_articles.json --translate --min-signal 3
   ```

4. Find output: `output/AI_News_YYYYMMDD_YYYYMMDD.docx`

**Typical runtime:**
- Collection: 1-3 minutes
- Deduplication: ~5 seconds
- Summarization: 4-7 minutes for 70-100 articles
- Word doc + translation + funding search: 5-10 minutes
- **Total**: ~15-20 minutes

**Typical cost per run:**
- Summarization (Claude): ~$0.50-1.00
- Translation (Claude): ~$0.50-1.00
- Funding search (OpenAI): ~$0.05-0.15
- **Total**: ~$1.00-2.00
