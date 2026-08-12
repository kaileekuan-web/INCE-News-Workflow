# Collect Consumer News (Bi-Weekly)

## Objective
Collect consumer tech news from WeChat public account articles, summarize in Chinese using Claude, and output a formatted Word document with two sections: (1) Consumer Tech News table and (2) Consumer Tech Fundraising News table sourced from a live Claude web search.

## Required Inputs
- **WeChat article URLs** in `wechat_urls.txt` (one URL per line, `#` for comments)
- **Start date** (YYYY-MM-DD) — used for the document header and funding search
- **End date** (YYYY-MM-DD)
- **`ANTHROPIC_API_KEY`** in `.env` — every inference in this workflow runs on Claude; see collect_ai_news.md for the optional `NEWS_CLAUDE_*` overrides
- **API keys** in `.env`:
  - `ANTHROPIC_API_KEY` — also powers the consumer funding news search (Claude with the server-side web search tool)

## Tools Required
1. `tools/collect_wechat.py` — Fetch WeChat articles from URL list
2. `tools/summarize_articles.py` — Summarize articles in Chinese via Claude
3. `tools/generate_consumer_doc.py` — Word document generation with funding section
4. `tools/utils.py` — Shared utilities (imported by other tools)

## Steps

### Phase 1: Prepare URL List

Before running, update `wechat_urls.txt` with the articles for this period:

```
# Consumer News — 2026-03-10 to 2026-03-23
# Source: 36Kr, LatePost, 晚点LatePost, 虎嗅, etc.
https://mp.weixin.qq.com/s/...
https://mp.weixin.qq.com/s/...
```

- One URL per line
- Lines starting with `#` are ignored
- Duplicate URLs are automatically skipped

### Phase 2: Data Collection

```bash
python tools/collect_wechat.py --urls wechat_urls.txt
```

- Output: `.tmp/raw_wechat.json`
- Fetches each article using a WeChat mobile browser User-Agent
- Extracts: title (`<h1 class="rich_media_title">`), date (`<em id="publish_time">`), content (`<div id="js_content">`)
- Falls back to `<body>` text if WeChat-specific selectors aren't found
- 1-second delay between requests to avoid rate limiting
- Expected: as many articles as URLs provided

### Phase 3: Deduplication

```python
python3 -c "
import json
articles = json.load(open('.tmp/raw_wechat.json'))
seen, unique = set(), []
for a in articles:
    url = a.get('url', '')
    if url and url not in seen:
        seen.add(url); unique.append(a)
json.dump(unique, open('.tmp/classified_wechat.json', 'w'), ensure_ascii=False, indent=2)
print(f'Deduped: {len(unique)} articles')
"
```

- Output: `.tmp/classified_wechat.json`

### Phase 4: Chinese Summarization

```bash
python tools/summarize_articles.py \
  --input .tmp/classified_wechat.json \
  --output .tmp/summarized_wechat.json \
  --provider claude --language zh --yes
```

- Uses Claude (`claude-opus-5` by default) with a Chinese-language prompt
- Generates 2-4 bullet point summaries in Simplified Chinese per article
- Fetches full article content before summarizing (falls back to description if fetch fails)
- `--yes` skips the cost confirmation prompt
- Output: `.tmp/summarized_wechat.json`
- Estimated cost: ~$0.30–0.60 for 30-50 articles

### Phase 5: Word Document Generation

```bash
python tools/generate_consumer_doc.py \
  --start_date 2026-03-10 --end_date 2026-03-23 \
  --articles .tmp/summarized_wechat.json
```

- **Section 1 — 消费科技新闻摘要**: 2-column table (日期 | 标题+中文摘要), sorted oldest → newest, titles hyperlinked
- **Section 2 — 消费科技融资动态**: 6-column table (Date | Company | Summary | Stage | Raise | Investors) via Claude web search
- Output: `output/Consumer_News_YYYYMMDD_YYYYMMDD.docx`
- Estimated time: 2-5 minutes
- Estimated cost: the funding section runs one Claude web-search turn per day in the range

## Full Pipeline (Quick Reference)

```bash
START_DATE="2026-03-10"
END_DATE="2026-03-23"

# Phase 2: Collect
python tools/collect_wechat.py --urls wechat_urls.txt

# Phase 3: Deduplicate
python3 -c "
import json
articles = json.load(open('.tmp/raw_wechat.json'))
seen, unique = set(), []
for a in articles:
    url = a.get('url', '')
    if url and url not in seen:
        seen.add(url); unique.append(a)
json.dump(unique, open('.tmp/classified_wechat.json', 'w'), ensure_ascii=False, indent=2)
print(f'Deduped: {len(unique)} articles')
"

# Phase 4: Summarize in Chinese
python tools/summarize_articles.py \
  --input .tmp/classified_wechat.json \
  --output .tmp/summarized_wechat.json \
  --provider claude --language zh --yes

# Phase 5: Generate Word doc
python tools/generate_consumer_doc.py \
  --start_date $START_DATE --end_date $END_DATE \
  --articles .tmp/summarized_wechat.json
```

Output: `output/Consumer_News_YYYYMMDD_YYYYMMDD.docx`

## Expected Outputs

**Primary Deliverable:**
- Word document: `output/Consumer_News_[start]_[end].docx`
  - **消费科技新闻摘要** table: all articles sorted oldest → newest, 2 columns (日期 | 超链接标题+中文摘要)
  - **消费科技融资动态** table: consumer tech funding events from Claude web search, 6 columns

**Intermediate Files (in `.tmp/`):**
- `raw_wechat.json` — fetched WeChat articles (title, date, content)
- `classified_wechat.json` — deduplicated articles
- `summarized_wechat.json` — articles with Claude-generated Chinese summaries

## Edge Cases & Error Handling

### WeChat Content Fetching

**WeChat returns empty content or login page:**
- **Symptom**: `title` is empty or content is a login prompt
- **Cause**: Some articles require WeChat app authentication (not publicly accessible)
- **Solution**: Open the article in a browser to confirm it's accessible, then re-add the URL
- **Workaround**: Script uses the URL as-is; if content extraction fails, summary falls back to the article description

**Timeout fetching article:**
- **Symptom**: `WARNING: Timeout fetching https://mp.weixin.qq.com/s/...`
- **Solution**: Script continues; that article uses its description for summarization
- **Prevention**: Increase `--delay` if getting frequent timeouts

**Date not extracted:**
- **Symptom**: `published_at` is empty in output JSON
- **Cause**: WeChat changed their HTML structure or article doesn't show date
- **Behavior**: Article still included; date column shows blank in Word doc
- **Fix**: Update `fetch_wechat_article()` in `collect_wechat.py` with new selectors

### Summarization

**Claude call failed:**
- **Symptom**: `WARNING: Claude call failed: ...` or `WARNING: ANTHROPIC_API_KEY not set in .env`
- **Solution**: Check the key in `.env` and that `anthropic` is installed; the script falls back to the article description otherwise. Re-run — completed articles are skipped, so only failures are retried.

**Summaries in English instead of Chinese:**
- **Check**: Confirm `--language zh` flag is set
- **Check**: Confirm article content was successfully fetched (some fallbacks use English descriptions)

### Funding Section

**Credit/quota error:**
- **Symptom**: a 400 about credit balance, or repeated `WARNING: Claude call failed`
- **Solution**: Top up at console.anthropic.com → Billing

**No funding events returned:**
- **Symptom**: Section shows "此日期范围内未找到消费科技融资事件"
- **Cause**: Legitimate (quiet period), or the search turn paused and gave up after 3 resumes
- **Solution**: Try running again, or manually check and add events

## Success Metrics

After each run, verify:
1. **Coverage**: Word doc contains all URLs from `wechat_urls.txt`
2. **Summary quality**: Spot-check 5 summaries — should be concise Chinese bullet points
3. **Dates**: Articles have dates populated (check for blank date column)
4. **Funding section**: Populated with relevant consumer tech events

## Typical Cost per Run
- Summarization (Claude): ~$0.03 per article at Opus 5 list rates
- Funding search (Claude web search): one multi-search turn per day in the range, plus $10 per 1,000 searches
- **Total**: scales with article count — set `NEWS_CLAUDE_MODEL=claude-sonnet-5` in `.env` for a cheaper run

## Lessons Learned

### 2026-08-12 Update (current)
- **Back on Claude, end to end.** Summarization, classification, translation, and the funding web search all run on the Anthropic API — `ANTHROPIC_API_KEY` is the only key this workflow needs. See the 2026-08-12 entry in `collect_ai_news.md` for the full change list and the `NEWS_CLAUDE_*` overrides.
- **The consumer-mode second-pass "editor" call is now opt-in** (`CONSUMER_REFINE=1` in the environment). It was added to compensate for a small local model; on Claude the draft prompt alone is enough, and the second pass doubled per-article cost.

### 2026-07-20 Update
- **Removed Claude/Anthropic entirely.** Summarization now runs on the local **Ollama** model (`qwen2.5:32b-instruct-q4_K_M` by default) instead of `api.anthropic.com` — zero per-run cost for this step. Only the optional OpenAI funding web search still costs anything. Expect a modest quality tradeoff vs. Claude on messy source HTML.
- WeChat remains the source for this workflow (unlike the AI news workflow, which switched to Twitter-only) since there's no equivalent automated multi-source setup to consolidate here.

### 2026-03-23 Initial Implementation
- WeChat article pages are publicly accessible via direct URL when using a mobile browser User-Agent
- WeChat HTML structure: title in `<h1 class="rich_media_title">`, date in `<em id="publish_time">`, content in `<div id="js_content">`
- Summarization uses `--language zh` flag added to `summarize_articles.py` which switches prompt to Chinese
- Consumer funding search uses a consumer-focused prompt (retail, e-commerce, consumer apps) vs AI news workflow's AI-focused prompt
- No translation step needed — WeChat articles are already in Chinese, summaries generated directly in Chinese
