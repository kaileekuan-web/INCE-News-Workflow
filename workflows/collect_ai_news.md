# Collect AI News (Bi-Weekly)

## Objective
Collect AI-related news from X/Twitter (the only source), summarize and translate to Chinese using Claude, and output a formatted Word document.

**The document has exactly three parts, in this order, and nothing else:**

1. **Header** — report title and the date range. No article counts, no generation timestamp, no source tallies. Those are facts about the pipeline, not news, and a reader opening a digest should meet the news first.
2. **AI News Summary** — AI stories only, and never a funding round.
3. **AI Fundraising table** — every round in the range, sourced as close to the announcement as possible.

Nothing sits below the fundraising table. Source-conflict notes that used to print there now appear in the row they qualify.

## Editorial rules (what belongs in the news table)
These four rules are enforced in code (`tools/news_filters.py`), at collection and again at document generation. They define the report:

1. **Smaller AI startups only.** Stories about frontier labs and big tech — the companies in `frontier_labs.txt`, which covers the frontier labs and the Mag 7 — are dropped. A startup story that *mentions* one is kept: "raised to compete with OpenAI" and "founded by ex-DeepMind researchers" are startup stories. The test is who the story is **about**.
2. **Events only — not opinion, not statements.** Two distinct things are excluded here:
   - *Opinion*: takes, predictions, threads about what an event means, engagement bait.
   - *Statements*: an executive saying something rather than doing something — conference remarks, interviews, podcasts, town halls, "the CEO warns/expects/believes". These are real and sourced and still not events.
   - The exception that keeps this from eating the report: an item that carries a **hard event** stays, even when it arrives as a quote. "Sierra's CEO said the company raised $350M" is a funding round being reported through a quote.
3. **No duplicates.** Four kinds are caught: the same link (in any spelling — http/https, www/m/amp, tracking parameters), identical wording, two wordings that still overlap heavily, and the case word overlap can't see — the company's announcement and the press write-up of it, which share the story's *names and figures* while sharing few words. Dedup runs three times: at collection, again after source resolution (which can reveal that two posts point at one article), and once more at document generation. The richest copy survives.
4. **Every entry links to a published article, never to X.** Most announcement tweets link nowhere, so `tools/resolve_sources.py` searches for the article covering the same event and **verifies it against the post** before accepting it. What still has no article after that is dropped from the news table (`--allow-unlinked` keeps it as an unlinked headline). The x.com URL is kept in the JSON as `x_url` for provenance only.

5. **No funding rounds in the news section.** A round is the fundraising table's subject, reported there with amount, stage, valuation and investors in named columns and cross-checked across five kinds of source. The same round retold as prose in the news section is the identical fact twice, in the worse of the two formats. `is_funding_story()` catches it on the headline and summary — deliberately not the body, because a launch story routinely mentions that the company "raised $30M last year".
6. **Primary sources over aggregators.** The report links to the announcement itself — the company's own blog, `/news` or `/press` page, its press release, or the lead investor's own post — and falls back to trade press only when no primary source exists. TechCrunch's writeup of a launch the company blogged about is one hop further from the facts, often paywalled, and downstream of the thing we could have linked to. Deal databases (Crunchbase, PitchBook, Dealroom, Tracxn) are compiled from announcements and are **never** primary. Enforced in `best_source_link()` and `AGGREGATOR_HOSTS` (`news_filters.py`), in the source-resolution prompt (`resolve_sources.py`), and in the funding search's own instructions.

**What "substantively about AI" means (news section):** the summarizer's `category` decides, not a keyword match — it read the whole article and a keyword match reads a headline. `模型与研究`, `产品与应用`, `政策与安全`, `行业动态` and `大科技公司` are in; `非AI` and `其他` (the filler bucket) are out. This matters: "Today we're launching Projects, an evolution of Spaces" is a Perplexity product launch containing no AI term at all, and a keyword gate drops it while happily keeping a funding post that says "AI" in passing.

Escape hatches: `--include-frontier`, `--include-opinion` and `--allow-unlinked` on the document generators (`collect_x.py` takes the first two). Rules 1 and 2 are also configurable without code: edit `frontier_labs.txt` to let a company back in.

## Required Inputs
- **Start date** (YYYY-MM-DD)
- **End date** (YYYY-MM-DD)
- **API keys** in `.env`:
  - `ANTHROPIC_API_KEY` — **required**. Every inference in this workflow runs on Claude: article summarization, relevance scoring, Chinese translation, executive summary, funding extraction from collected posts, and the funding web search.
- **Optional Claude overrides** in `.env` (all `NEWS_`-prefixed so they can't collide with the `CLAUDE_*` vars a Claude Code shell already sets):
  - `NEWS_CLAUDE_MODEL` — default `claude-opus-5`; set to `claude-sonnet-5` or `claude-haiku-4-5` to trade quality for cost on large runs
  - `NEWS_CLAUDE_EFFORT` — default `low` (thinking depth for the per-article calls); raise to `medium`/`high` if summaries read thin
  - `NEWS_CLAUDE_MAX_TOKENS` — default `4096`; the floor applied to every call's token budget
  - `NEWS_MAX_WORKERS` — default `6`; how many API calls run at once. This is the main speed lever: summarization, the funding day-search, translation and source resolution all run through it. Set to `1` for the old strictly-sequential behaviour (slower, but the log reads in order — useful when debugging)
  - `X_FETCH_TIMEOUT` — default `8` seconds per Nitter mirror request
- **Editorial config** (repo root, editable, no code change needed):
  - `frontier_labs.txt` — the companies excluded from the news table, one per line with their aliases, X handles and domains
- **X/Twitter source** (the primary and only automated source):
  - `x_accounts.txt` - handles + search queries to follow (repo root, editable)
  - Account timelines come from free Nitter RSS mirrors (no key). When a mirror won't serve an account, that account falls back to Claude web search — so a Nitter outage degrades the report instead of emptying it
  - `NITTER_INSTANCES` in `.env` (optional) - comma-separated Nitter instances to override the built-in defaults when they are down

## Tools Required
1. `tools/collect_x.py` - X/Twitter posts via Nitter RSS, with a Claude web-search fallback (accounts + searches from `x_accounts.txt`)
2. `tools/collect_x_claude.py` - the fallback itself; also runnable standalone to collect without Nitter at all
3. `tools/news_filters.py` - the four editorial rules above (frontier labs, opinion/statements, dedup, source links). Imported by every other tool here; run it directly (`python tools/news_filters.py`) for its self-tests
4. `tools/dedup_articles.py` - merge collector output into one deduplicated file
5. `tools/resolve_sources.py` - find and verify the published article behind a link-less post
   - Self-tests for the funding consolidation live in the document generator: `python tools/generate_word_doc.py --self-test`
6. `tools/summarize_articles.py` - Fetch full content and generate summaries via Claude
7. `tools/generate_word_doc.py` - Word document generation with translation and funding section. Also hosts the shared pieces both generators use: `add_report_header()`, `create_funding_table()`, `FUNDING_SOURCE_PASSES` and the funding search
8. `tools/generate_ai_doc.py` - the AI news doc the webapp builds (same rules, different layout). Both generators now share one header, one funding table and one editorial line — `filter_news_section()` lives in `news_filters.py` precisely so the two can't drift apart
9. `tools/utils.py` - Shared utilities (imported by other tools; also hosts the `call_claude` / `translate_to_chinese_claude` / `get_claude_client` helpers)

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
   - **The editorial rules are applied here first**, because a post dropped at collection is an LLM call not spent on it. The run prints what went and why: `Filtered out: promo=…, opinion=…, frontier=…` plus a `Frontier/big-tech coverage dropped: OpenAI (45), Anthropic (29)…` line. Expect the frontier count to be large — on a two-week range it was 104 of 247 articles
   - Each post carries `source_url`: the link the tweet pointed at, resolved through t.co. The run reports `Source links: N/M posts link to the news behind them` — the rest will appear in the report as unlinked headlines
   - **Read the health line.** Every run prints `Nitter health: N/M accounts served, X unserved, Y empty feed(s)` and names the unserved accounts, so a low yield is explainable instead of silent
   - Fallback items are labelled `X/Twitter (via Claude search)` and carry `via_claude_search: true`. They are **coverage of what an account announced, not the tweet itself** — x.com blocks automated readers, so the URL points at the source that verifies the claim
   - Expected: 50-150 posts per 2-week range. The step **exits non-zero on 0 posts**, which stops the pipeline at the real cause rather than producing an empty document three phases later

   **Flags:**
   - `--fallback auto` (default) — Claude fallback only when the run looks degraded (an account went unserved, or the whole run came back under `--min-posts`)
   - `--fallback always` — chase every account Nitter produced nothing for, even on a healthy run. Widest coverage, highest cost
   - `--fallback never` — Nitter only, zero API spend in this step; accept that an outage means an empty report
   - `--min-posts N` (default 20) — the "degraded" threshold for `auto`. `--min-posts 0` means only unserved accounts trigger the fallback
   - `--include-frontier` / `--include-opinion` — turn off editorial rules 1 and 2 for this run

### Phase 3: Deduplication

4. **Deduplicate articles:**
   ```bash
   python tools/dedup_articles.py --inputs .tmp/raw_x.json --output .tmp/classified_articles.json
   ```
   - Output: `.tmp/classified_articles.json`
   - Four kinds of duplicate are collapsed and counted separately: `same-url` (tracking parameters, http/https, www/m/amp all normalized first), `same-text`, `near-text` (word overlap ≥ 0.55), and `same-story`. The copy with a real link — and, failing that, the longer one — survives
   - **`same-story` is the one that matters most.** Measured on real posts, the company's announcement and the press write-up of the same launch share only 0.27 of their words — no overlap threshold can catch that without also merging unrelated posts, which sit in the same band. So it matches on the story's *markers* instead: multi-word names, proper nouns and normalized figures. Two shared markers are required, at least one of them a name or a figure, plus a low bar of word overlap. Abundant evidence (three markers) relaxes the overlap bar further, which is what catches two wordings of one funding round
   - `collect_x.py` already ran this pass over its own output; running it again is what merges multiple collectors (X + WeChat) without letting the overlap through

### Phase 3.5: Source Resolution

5. **Find the news article behind each link-less post:**
   ```bash
   python tools/resolve_sources.py --input .tmp/classified_articles.json --yes
   ```
   - Input/output: `.tmp/classified_articles.json` (updated in place)
   - **Why this exists**: most announcement tweets link nowhere — on a real run only 2 of 20 surviving posts carried a link. Without this step those items are unlinked headlines summarized from 200 characters of tweet
   - Asks Claude, with web search, which published article reports the *same event* (not merely the same company), in batches of 4 posts per search call
   - **Every proposed link is verified before it is accepted**: the page is fetched and scored against the post's own wording (`news_filters.corroborates`, default threshold 0.45). Below that it is discarded and the post stays unlinked. A search will confidently return an article about the right company and the wrong event, and a link that doesn't support the summary under it is worse than no link
   - Accepted items get `source_url`, `source_title`, `source_publisher`, `source_verified` and `source_match_score`. x.com URLs are rejected outright even when the model returns one
   - Knock-on benefit: Phase 4 then summarizes **the article** instead of the tweet, so entries carry the figures, investors and product detail the post omitted
   - Cost: roughly $0.05 per batch (one search turn plus the verification fetch) — about $0.25-0.50 on a typical run. Prompts above $2 unless `--yes`
   - Flags: `--max N` for a cheap trial run, `--threshold` to tune strictness, `--no-verify` to accept search results unchecked (not advised)

### Phase 4: Article Summarization

6. **Summarize collected posts:**
   ```bash
   python tools/summarize_articles.py --provider claude --yes
   ```
   - Input: `.tmp/classified_articles.json`
   - Uses **Claude** (`claude-opus-5` by default, effort `low`) to generate concise paragraph summaries, category, relevance score, VC signal type, and two fields the document generators filter on:
     - `subject_type`: `frontier_lab` | `big_tech` | `startup` | `other` — who the story is **about**. This is the judgement the keyword filters at collection can't make ("startup raises to compete with OpenAI" is a startup story)
     - `content_type`: `news` | `opinion` — whether it reports an event at all
     - Both default to the permissive value when the model's output can't be parsed, so a formatting wobble never silently empties the report
   - **When a post links to an article, that article is fetched and summarized**, not the tweet — the announcement has the figures, investors and product detail a 200-character post leaves out. If the page returns less than 400 characters (paywall, JS-only page), the captured post text is used instead and the run says so
   - X posts with no link still have their full tweet text captured at collection time, so they are summarized/scored directly with no fetch
   - Output: `.tmp/summarized_articles.json`
   - **Accuracy controls**: summary length scales to source length; prompts forbid outside knowledge and unsourced figures; every figure is arithmetically checked against the source, with one targeted repair pass when a mismatch is found. Watch for the `⚠ GROUNDING` block at the end of the step — anything listed there is an article to eyeball before sending
   - Cost: roughly $0.03 per post at Opus 5 list rates — the script prints an estimate and asks for confirmation above $2 unless `--yes` is passed
   - Estimated time: ~5-15s per post (network-bound, one request each); progress is saved after every article, so a re-run resumes rather than repeats

### Phase 5: Word Document Generation

7. **Generate Word document with Chinese translation and funding section:**
   ```bash
   python tools/generate_word_doc.py --start_date 2026-07-06 --end_date 2026-07-20 \
     --articles .tmp/summarized_articles.json --translate
   ```
   - Input: `.tmp/summarized_articles.json`
   - **Curation runs first and prints its work**, so the article count in the doc is explainable:
     ```
     Duplicates removed: 5 (same-text=4, near-text=1)
     Out of scope: 205 article(s) — no event reported (92), OpenAI (45), Anthropic (29), …
     News table: 37 article(s) about smaller AI startups
     ```
     It runs here as well as at collection because this is the last point where every article is in one place — hand-added items, WeChat articles and anything collected before these rules existed all pass through it, and only here are `subject_type` / `content_type` available
   - **Header** — title and date range only. The `生成时间 · 文章总数` metadata line and the `共 N 篇` count under each heading are gone: they were build output printed as if they were the lead. Both counts still print to the console during the run, where they are useful.
   - **Watchlist** — matches are reported on the console (`Watchlist: 4 article(s) matched (Harvey, Sierra)`), not rendered as a section. A second news table above the first one competes for the top of the page, and the report's structure is header → news → fundraising and nothing else. Pass `--watchlist-section` to put it back.
   - **Section 1 — AI News Summary**:
     - **Scope, applied in this order** (`filter_news_section()` in `news_filters.py`, shared by both generators so the two layouts can't drift): funding rounds out → not substantively AI out → below `--min-signal` out. The console names one reason per dropped article: `News section scope: removed funding (25), low signal (3)`
     - `--min-signal` **defaults to 3** on both generators now (it was 1 on `generate_word_doc.py`), which is what cuts filler. Pass `--min-signal 1` to keep everything
     - **Headlines are cleaned before rendering** (`clean_headline()`): the `@handle:` prefix, inline `@mentions`, hashtags, thread markers (`🧵`, `👇`, `1/7`), decorative emoji and the trailing `…`/`$...` left by X's character limit. `@perplexity_ai: Today we're launching Projects…` renders as `Today we're launching Projects`. Without this the report reads like a screenshot of a timeline
     - **Links go to the primary source** — see editorial rule 6
     - `generate_word_doc.py` renders Date | 优先级 (1-5) | Summary grouped by category with VC signal badges (`[融资]` `[产品]` `[合作]` `[人事]` `[监管]` `[研究]`); `generate_ai_doc.py` renders a flat Date | Summary table, oldest first
   - **Section 2 — AI Fundraising News table** (7 columns):
     - Columns: Date | Company | 概述 (+ source links) | Stage | Raise | Valuation | Investors
     - **The 优先级 column is gone.** It was a stage lookup dressed as editorial judgement — "seed → 重点关注" is a rule the reader can apply from the 轮次 column they are already reading, and printing it as a verdict gave a lookup table the authority of a recommendation
     - **Primary source**: Claude extracts structured funding events from collected posts
     - **Supplemental source**: Claude with the server-side `web_search` tool, **one search turn per day per source pass** so the announcement date is exact (skipped entirely if `ANTHROPIC_API_KEY` is unset)
     - **Five source passes per day, not one** (`FUNDING_SOURCE_PASSES` in `generate_word_doc.py`). One search per day found the rounds one feed happened to carry, and the rounds it missed were not random — they were the small ones, the non-US ones, and the ones announced by filing or by an investor's blog post rather than by an article. Each pass searches a different kind of source, and they overlap by design:

       | Pass | Looks in |
       |---|---|
       | `primary` | Company blogs, `/news` and `/press` pages, Business Wire / PR Newswire / GlobeNewswire, and VC firms' own posts about rounds they led |
       | `newsletters` | Axios Pro Rata, Fortune Term Sheet, StrictlyVC, PE Hub, Newcomer — **including the deal lists at the bottom of each issue**, which is where small rounds live |
       | `databases` | Crunchbase News daily roundups, PitchBook, Dealroom, Tracxn, CB Insights — compiled rather than reported, so they include rounds nobody wrote an article about |
       | `filings` | SEC Form D on EDGAR and equivalent registries. A Form D filed on the date is a disclosed round even when no publication covered it |
       | `trade` | TechCrunch funding/venture tags, VentureBeat, Sifted, Tech.eu, EU-Startups, FinSMEs, Reuters, Bloomberg, plus regional outlets (Calcalist, Globes, Nikkei, TechNode, LatamList) |

     - **Cross-check**: `_merge_funding_events()` collapses copies of one round into the entry with the most disclosed fields, and the run prints how many rounds two or more *kinds* of source agreed on: `Corroboration: 14/19 confirmed by 2+ source kinds`, naming the single-source ones. A single source is **not** a reason to drop a round — a Form D nobody wrote about is still a round — but a table that is mostly single-source means the passes aren't overlapping, which is how rounds got missed before
     - **Sourcing stays close to the announcement**: `_prefer_primary_source()` reorders each row's links so the company's or investor's own post is first and becomes the row's hyperlink; aggregators fall to the back
     - `--funding-passes primary,newsletters,databases` runs a subset. **Fewer passes means missed rounds** — this is a cost lever, not a tuning knob
     - **The search works to a brief** (`_search_funding_single_day` in `generate_word_doc.py`):
       - Includes a round only if it was **first publicly announced** on that date — not the publication date of a recap or later coverage
       - Excludes rounds from other dates, recaps of earlier rounds, unicorn lists, funding roundups, market/trend pieces, rumours and unconfirmed reports. **A day with nothing returns nothing** — substituting a nearby date silently widens the report's range
       - Verifies against two reputable sources where they exist and lists every URL used; conflicts go in `discrepancy` rather than being resolved silently
       - `"Not disclosed"` is the convention for anything no source states — **and only that**. Without the second half of that instruction the model marked stage and lead investor unknown on rounds whose own source named both
       - **One credible source is enough to report a round**; two are for confirming its details. A round found only in a filing, only in a database, or only in the investor's post is reported with what that source states and `"Not disclosed"` for the rest. The old "verify against two sources" instruction, read strictly, dropped exactly the small rounds the extra passes exist to find
       - Chinese summaries: 2-3 sentences, professional, with varied openings (product, technology, market, the round, use of funds) instead of "这是一家…"
     - **Rendering**: source links appear under each Chinese summary as `来源 [1] anthropic.com · [2] techcrunch.com`, primary first. A conflict between sources prints **in its own row** as `来源存在出入：…` rather than in a block under the table — a footnote at the bottom of the section is read by nobody looking at the row it qualifies
     - **Consolidation** merges reports of the same round — see the self-test below, because this is where the subtle bugs live:
       - The entry keeps the **earliest** announcement date. A recap two days later is not a new announcement, and letting its date win because its article happened to name the valuation files the round under the wrong day
       - Fields are filled from whichever copy discloses them, not taken wholesale from the richer one — two outlets rarely disclose the same subset
       - Two entries merge only if they **look like the same round**: agreement on stage or amount settles it; failing both, entries within three days are treated as one round. Company name alone is not enough — a seed in March and a Series B in August are one company and two events
       - Run `python tools/generate_word_doc.py --self-test` to check all of this
   - Output: `output/AI_News_20260706_20260720.docx`
   - Estimated cost: the funding web search is the expensive part, and it is now **5× what it was** — days × 5 passes rather than days × 1, plus $10 per 1,000 searches. A five-day range is 25 search turns. That is the price of not missing rounds; trim it with `--funding-passes` if a run doesn't need full coverage

   **Flags:**
   - `--min-signal N` — only include articles with relevance ≥ N (1=all, **3=default**, 4=high-signal only)
   - `--funding-passes KEYS` — comma-separated subset of `primary,newsletters,databases,filings,trade`. Default: all five
   - `--watchlist-section` — render the watchlist as its own section again (off by default)
   - `--watchlist FILE` — path to watchlist file (default: `watchlist.txt`)
   - `--include-frontier` — put frontier-lab and big-tech stories back in
   - `--include-opinion` — put commentary back in

## Expected Outputs

**Primary Deliverable:**
- Word document: `output/AI_News_[start]_[end].docx`
  - **Header**: title + date range only
  - **AI News Summary** table: AI stories only, no funding rounds, headlines cleaned of tweet formatting, links to primary sources
  - **AI Fundraising News** table (7 columns, no 优先级): funding events extracted from collected posts + supplemented via a five-pass Claude web search per day
  - Nothing below the fundraising table

**Intermediate Files (in `.tmp/`):**
- `raw_x.json` - X/Twitter posts collected via Nitter RSS (source `X/Twitter`), each with `source_url` (the news behind the post) and `x_url` (provenance)
- `classified_articles.json` - Deduplicated posts
- `summarized_articles.json` - Posts with Claude-generated summaries, plus `subject_type` and `content_type`

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
1. **Coverage**: 50-150 posts collected before filtering; expect a much smaller news table after it (roughly 15-40), since frontier-lab coverage and commentary are the bulk of a raw X timeline. 0 collected is possible if Nitter is down — re-run
2. **Scope**: skim the news table for a frontier-lab story that got through, and for a startup story wrongly dropped (the `Out of scope` line names every company that was removed and why). Fix either by editing `frontier_labs.txt`
3. **Summary Quality**: Manually review 10 random summaries
   - Target: Clear, concise, captures key points, no invented entities or figures
4. **Links**: headlines link to articles, never to x.com. Unlinked headlines are expected — they are posts that linked nowhere
5. **Runtime**: about 8-10 minutes end to end on a 2-week range. Measured on a real run: collection 2.5 min of Nitter plus ~2 min of topic searches, source resolution ~7 min, summarization ~1 min, funding search ~5 min, document 15s. If a run takes much longer, read the `⏱` phase lines in the log — they name the phase that owns the time rather than leaving you to guess
6. **Cost**: single-digit dollars per run — dominated by per-article summarization and the day-by-day funding web search. Filtering at collection cuts this substantially, since dropped posts are never summarized
7. **Data Quality**: No duplicate posts, retweets excluded, no opinion pieces

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

### 2026-08-14 Update — the report is three parts, and the funding search stopped missing rounds (current)

A review of the 8/10–8/14 report found five things wrong, and fixing them touched both generators plus the shared filters.

**The report opened with its own build log.** `文章总数：33` and `共 33 篇` were the first two lines a reader met. They are facts about the pipeline, not news. Both are gone from the document and print to the console instead, where they were always the useful place for them. Same for the `生成时间` stamp on `generate_word_doc.py`.

**The headlines were tweets.** `@perplexity_ai：今天我们推出 Projects……` — handle, hashtags, thread marker and the ellipsis where the post ran out of characters, all rendered verbatim. `clean_headline()` strips them. Worth noting *where* the bug was: the document header (`AI 资讯报告` + date range) was already clean; what read as copy-pasted from X was every item headline under it. The fix is in the row renderer, not the masthead — though the masthead is now shared and formats the range properly (`2026年8月10日 – 2026年8月14日`).

**Three quarters of the news section was funding news.** On the last live dataset, 25 of 33 curated stories were funding rounds — the same rounds the fundraising table reports with better columns and wider sourcing. `is_fundraising_article()` had been hard-disabled (`return False`, "all articles go to the main news table"), so nothing separated them. Re-enabled via `is_funding_story()`, which reads the headline and summary and deliberately **not** the body: a launch story routinely mentions the company "raised $30M last year", and matching on the body moved product launches into the funding bucket.
- Two false positives caught while testing, both instructive: bare `raised` matched *"MoK raised throughput 40%"* (a kernel benchmark), and bare `估值` matched a VC describing its accelerator's terms. Neither word survives on its own now — a raise without a round name has to appear next to a figure.
- **The consequence is uncomfortable and worth stating**: the news section went from 33 items to 5. That is the requested behaviour working, but it also means the X-sourced collection is almost entirely funding news. If the news section should be fuller, the fix is upstream in `x_accounts.txt`, not in the filters.

**"Substantively about AI" cannot be a keyword test.** The first attempt used `is_ai_relevant()` and immediately dropped *"Today we're launching Projects, an evolution of Spaces"* — a Perplexity product launch containing no AI term — while keeping funding posts that said "AI" once in passing. The summarizer's `category` is the right judge: it read the whole article. Keyword matching is the fallback for articles that have no category.

**The funding search relied on one feed.** The 8/10–8/14 run missed Echovane, Point2 Technology, Discovered Materials, River AI, Legio, Palette, CodeRabbit, Skan AI, Yuno, Remepy, Silicon Data and Mindgard. Every one was public somewhere — a database entry, a Form D, an investor's own post — just not where the single search looked. The day is now searched five times over, once per kind of source (see the table in Phase 5), and the results are merged and corroboration-counted. Two supporting changes mattered as much as the extra passes:
- The old *"verify each round against at least two reputable sources"* instruction, read strictly, **dropped exactly the small rounds the new passes exist to find**. One credible source now reports a round; the second is for confirming details.
- `_prefer_primary_source()` reorders each row's links so the company's or investor's own post is the hyperlink. Aggregator coverage is a report of the announcement; the report wants the announcement. `AGGREGATOR_HOSTS` in `news_filters.py` is the list, and deal databases are on it — Crunchbase is compiled *from* announcements and is never primary.
- **Cost**: this is 5× the funding search spend. A five-day range is 25 search turns, not 5. `--funding-passes` trims it, with the obvious trade.

**Everything below the fundraising table is gone**, and the watchlist section above it with it. The source-conflict notes that used to print underneath moved into the rows they qualify — a footnote at the bottom of a section is read by nobody looking at the row it applies to. The `优先级` column came out of the fundraising table too: "seed → 重点关注" is a lookup the reader can do from the 轮次 column, and printing it as a verdict gave it the authority of a recommendation.

**Still open:** the 12 missed companies above have not been re-verified against a live run — that needs a paid funding search over 8/10–8/14. The `优先级` (1-5 relevance) column in `generate_word_doc.py`'s *news* table is untouched; it is a different column from the fundraising one that was removed, but it shares the label.

### 2026-08-13 Update — the funding search now works to a brief
- **The funding section is written to a specification, not a topic.** It used to ask for "events announced on DATE" and take what came back; it now filters on the **first public announcement date**, names the publications to search, excludes recaps/roundups/unicorn lists/rumours, requires two-source verification, and returns an empty day rather than reaching for a nearby one. The per-day loop is what makes the date exact.
- **"Not disclosed" needed a second half.** Told only that undisclosed details should read "Not disclosed", the model used it for anything it hadn't bothered to extract — stage and lead investor came back unknown on rounds whose own source named both. Adding *"'Not disclosed' means no source states it — not that you did not look"* turned Lovable from two blanks into `Series C` / `Menlo Ventures (lead)`.
- **Three consolidation bugs, all found by feeding the merge realistic inputs rather than reading it.** Each one is now a self-test (`--self-test`):
  - A recap moved the round's date. The richer copy won wholesale, so a fuller article published two days later carried its own date in — filing the round under the day it was recapped. The consolidated date is now always the earliest.
  - Details disclosed only by the losing copy were discarded. Fields are filled individually now.
  - **Two different rounds for one company merged into one.** Consolidation keyed on company name alone, so a seed in March and a Series B in August collapsed into a single entry showing the seed — a round deleted and the survivor mislabelled. Entries must now agree on stage or amount, or fall within three days of each other.
- **Web search is not reproducible.** The same date searched twice returned four events, then two. Nothing is wrong; it means the funding table legitimately differs between reruns of one range, and a missing round is worth re-running before it is worth debugging.
- **Verified live** on 2026-08-11/12: varied Chinese openings, English columns, up to five sources per round, and a genuine conflict surfaced — a PR Newswire dateline of Aug 10 against reprints dated Aug 11, which is exactly the recap-versus-announcement trap the brief targets.

### 2026-08-13 Update — first live run end to end

Run: 2026-07-30 → 2026-08-13. 75 posts collected, 33 in the report. Measured phase times, all with `NEWS_MAX_WORKERS=6`:

| phase | time | notes |
|---|---|---|
| Collection (Nitter) | 2.5 min | 29/29 accounts served, 54 posts kept |
| Collection (6 topic searches) | **12 min** | sequential — since fixed, expect ~2 min |
| Dedup | instant | |
| Source resolution | 6.8 min | 9 search calls, 6 at a time |
| Summarization | **1.1 min** | 38 articles, 1.7s each — was ~6 min sequential |
| Funding search | 5 min | 15 days, 6 at a time |
| Document | 14 s | translation skipped: summaries already Chinese |

- **The searches nobody had parallelised owned the run.** Collection spent 12 of its 15 minutes on six web searches run back to back. The funding search and source resolution had been parallelised; the `search:` topic lines and the account-fallback batches had not. Both do now — that alone is ~10 minutes.
- **`--curate` cut 72 articles to 38 before summarization**, so a third of the run's LLM spend simply never happened. Worth knowing the trade: the summarizer's `subject_type` / `content_type` judgement never runs on what it drops, so those drops rest on the deterministic filters alone. `--allow-unlinked`, or dropping `--curate`, shows everything.
- **Two duplicate pairs still reached the report, and only reading the output found them.** Both were the same shape — one event, two publishers:
  - The same pre-seed round twice: the amounts differed ($4.95M vs €4.29M) and each cited a different lead investor, so the source texts shared only the company name. Both *summaries* carried the founder's name and the amount. Markers now come from the summary too — it is a normalized account of the event, so two write-ups converge there when the source wording doesn't.
  - A partnership announced by both parties (the company's post, the university's quote): 0.029 word overlap. Overlap is now scored on summaries as well, whichever agrees more, and the three-marker floor moved 0.12 → 0.10. Checked before changing: across all 561 pairs of the live report, summary overlap between unrelated stories has median 0.031 and p90 0.058, and any floor from 0.08 to 0.12 gives an identical result — the marker gate is the discriminator, the floor is a sanity check.
- **`\b` never matches between a CJK character and a Latin capital.** In `加拿大AI公司Cohere与滑铁卢大学` — ordinary phrasing in a Chinese summary — the company name produced no marker at all, leaving Chinese text partly invisible to dedup. Proper-noun and name-phrase matching now use explicit boundaries.
- **Verification is earning its keep.** `resolve_sources` recovered 16 of 36 link-less posts and *rejected 5* whose article didn't corroborate the post (overlap 0.07-0.44) — those would have been wrong links under a plausible headline. It also surfaced 3 duplicates that were invisible until both posts pointed at the same URL.
- Final audit of the report: 0 frontier/big-tech stories, 33/33 `content_type: news`, 33/33 linked to published coverage, **0 x.com links**, dedup idempotent, 0 ungrounded figures (2 fired mid-run and were repaired).

### 2026-08-13 Update — debug pass on the whole pipeline
- **The most valuable item type in the report was being silently dropped.** "Harvey raised a $300 million Series E led by Kleiner Perkins" was rejected as `not-ai`: the post says what happened, not what the company does, and the topic filter demands the word. Found by running `collect_x.py` end-to-end against a stubbed Nitter for the first time — the module had never actually been executed in a test. A post with a **hard event and a real figure** now survives the topic test, marked `topic_unverified`; the summarizer, which reads the whole article, answers "is this an AI company?" with a new `非AI` category, and curation drops those. A regex cannot know what Harvey does; a reader of the article can.
- **The timing logging I added last round crashed three pipelines on success.** Deeptech, consumer and crypto never call `_phase()`, but the completion block read `jobs[job_id]["_phase_started"]` directly — a `KeyError` caught by the pipeline's `except`, reporting a failed run for a report that was finished and on disk. Now one defensive `_log_timings()` helper, and every pipeline's phases are timed rather than just two.
- **Hand-picked articles are exempt from the editorial filters.** With `--curate` running before summarization, a WeChat URL pasted by hand could be dropped as a "statement" and never appear — silently discarding an explicit instruction. Anything from WeChat (or flagged `manual`) now bypasses the filters while still going through dedup.
- **`--curate` requires a source link**, matching what the document generators do, so the two agree on what will be printed. Both pipelines that pass it run `resolve_sources.py` first.
- Smaller ones from the same pass: the Anthropic client is created under a lock (six threads raced to build it on the first batch); ordinary calls get a 180s timeout instead of the client's 600s, which is sized for web-search turns and let one stuck article hold a worker for ten minutes; and a failed summarization now falls back to the description instead of rendering an empty cell.

### 2026-08-13 Update — the run took an hour, and why
- **Almost none of the hour was computation; it was waiting one request at a time.** A bi-weekly run makes ~150 Claude calls of 5-15s each plus one web search per day in the range, and every one of them was sequential. `tools/utils.parallel_map` now runs them `NEWS_MAX_WORKERS` (default 6) at a time, preserving input order — order matters because each result is written back into a list or a document position.
- **Where the time actually went**, and what each fix is worth on a 14-day run:
  - *Summarization* — 1 call per article, sequential: **10-25 min → 2-4 min**.
  - *Funding search* — one web-search turn **per day in the range**, plus a 1s sleep between days: **7-15 min → ~1 min**. The days were always independent; the loop bought nothing but ordering, which `parallel_map` preserves anyway.
  - *Translation* — 2 calls per article made **inside the document render loop**, with 4s/8s retry sleeps: **4-7 min → under 1 min**. Now translated up front in one concurrent batch, then read from a cache while the document is built (python-docx is not thread-safe, so the render itself stays sequential).
  - *Nitter collection* — each dead mirror cost `2 user agents × 15s`. A timeout now stops trying that instance immediately (a different user agent cannot fix an unreachable host) and the timeout is 8s: **up to 96s → 16s per unserved account**.
  - *Source resolution* — batches now run concurrently.
- **Curate before summarizing** (`summarize_articles.py --curate`). The document generators drop frontier labs, opinion, statements, duplicates and unlinked items at the end — every one of those had already cost an LLM call and 5-15 seconds. The deterministic rules can run before summarization, and that is most of the volume. This is a cost saving as much as a speed one.
- **Resume was quietly re-paying for finished work.** It keyed only on `url`, so any article without that exact field was re-summarized on every re-run. The key now falls back `url → source_url → title`.
- **Every phase is timed now.** The webapp logs `⏱ [5/7] … took 43s` per phase and a total at the end, so the next slow run identifies its own culprit instead of needing this analysis again.

### 2026-08-13 Update — duplicates that word overlap cannot see
- **The duplicates that survive are the ones that don't share words.** On the real collected file, the same event written up by the company and by the press sat at **0.27** word overlap; unrelated posts sat at 0.30-0.33. There is no threshold that separates those, so lowering the existing one would have merged unrelated stories instead. The fix is a second, orthogonal signal: `story_markers()` — multi-word names, proper nouns, and figures normalized through `tools/grounding` so `$350M` and `350 million` are one marker.
- **The rule was derived from the false positives, not invented.** First attempt (one shared name) merged an availability announcement with a quote-tweet reacting to it. Second attempt (two shared markers) also merged two different products launching on the same platform — "Runway" and "agent" are two markers and no evidence — and double-counted `Opus 4.8` as both a phrase and a bare word. What survives contact with the data: **≥2 shared markers, at least one a name or figure, plus word overlap ≥ 0.25** — with the overlap bar dropping to 0.12 when three markers are shared, because "Sierra raised $350 million led by Greenoaks" and "Sierra lands a $350M round… Greenoaks leading" share the company, investor and amount but only 0.167 of their words. All seven cases, true and false, are now self-tests.
- **A generic-marker stoplist is not optional.** Without it, "Sierra raised $350M Series C led by Kleiner" and "Harvey raised $200M Series C led by Sequoia" share "Series C" plus funding boilerplate and read as one story — two different rounds, silently merged into one entry.
- **Two bugs found by running it on real data.** The name-phrase regex ran across sentence boundaries, inventing the name "greenoaks. sierra" and costing "Sierra" its own marker. And `canonical_url` preserved the scheme, so `http://` and `https://` spellings of one article were two articles.
- **A swallowed ImportError silently degraded dedup.** `story_markers` imported `tools.grounding` inside a `try/except: pass`; running the module directly puts `tools/` on `sys.path` rather than the repo root, so the import failed and **every figure marker silently disappeared**. The only symptom was one self-test passing under import and failing under direct execution. The import is now at module level, where breaking it is an error rather than a quiet loss of accuracy.
- **Dedup now runs after source resolution too.** Resolution is what makes two link-less posts about one event both point at the same article; collapsing them there means the duplicate is never summarized, and an LLM call per duplicate is saved.

### 2026-08-13 Update — statements, and finding the article behind a post
- **A CEO saying something is not news.** `is_statement()` catches conference remarks, interviews, podcasts, town halls and executive predictions, and the summarizer's `content_type` gained a third value (`statement`) alongside `news` and `opinion`. The rule that keeps this from eating the report is the redemption clause: an item carrying a **hard event** stays even when it arrives as a quote, so "Sierra's CEO said the company raised $350M" is still a funding round. `HARD_EVENT_SIGNALS` is deliberately narrower than `NEWS_SIGNALS` — 'report' and 'research' appear in half of all commentary and would redeem everything.
- **`tools/resolve_sources.py` closes the link gap, which was the biggest quality problem left.** Only 2 of 20 surviving posts on a real run carried a link, so the rest could only ever be unlinked headlines summarized from 200 characters. This step asks Claude with web search which published article covers the *same event*, in batches of 4.
- **The verification is the tool.** A search will confidently return an article about the right company and the wrong event. Every proposed URL is fetched and scored against the post's own wording (`corroborates()`: the share of the post's distinctive words appearing in the article, threshold 0.45 — not Jaccard, because the article is 50× longer and symmetric overlap always looks small). Below threshold the link is discarded and the post stays unlinked. A link that doesn't support the summary under it is worse than no link. x.com URLs are rejected outright even when the model returns one despite being told not to.
- **Resolution pays for itself twice.** Beyond the link, `summarize_articles.py` then fetches and summarizes *the article* rather than the tweet — so entries carry the figures, investors and product detail a post omits. That is most of what "more in-depth" costs.
- **Unlinked items are now dropped by default** (`--allow-unlinked` to keep them). After a resolution pass, an item no publication covered is a tweet, not news. The drop is printed with a pointer to `resolve_sources.py`, so this can never look like articles vanishing for no reason.
- **Tesla was the Mag 7 gap.** The list had six of seven; a Tesla/Optimus story would have sailed through.

### 2026-08-13 Update — startups only, news only
- **The report is now scoped to smaller AI startups.** `frontier_labs.txt` lists the excluded companies with their aliases, handles and domains; `tools/news_filters.py` enforces it at collection and at document generation. On a real two-week range this removed 104 of 247 articles — OpenAI (45), Anthropic (29), Google DeepMind (17), Mistral (8), Meta (7).
- **"About" is not "mentions".** The first version dropped *"Reflection AI, founded by two former DeepMind researchers, launched…"* — a startup story, thrown out for naming a lab. The fix is a context guard: a lab name introduced by `former`, `ex-`, `founded by`, `backed by`, `to take on`, `powered by`, `vs` and about thirty other phrases is background, not the subject. The name also only counts in the first 70 characters; anything later is context by construction. Both cases are in the self-tests.
- **Two boundary bugs, both found by running the filters on real data rather than fixtures.** The alias matcher compiled its word-boundary guard as `(?=X|)`, which always succeeds — so it was enforcing nothing, and the self-test that "proved" it worked passed for an unrelated reason. And the lab list only held AI-division names (`google ai`, `meta ai`), so "Google released Nano Banana 2 Lite" and "Meta is building…" walked straight through. Bare big-tech names are now listed, with a hyphen guard so `Google-backed startup raises $20M` stays in.
- **Opinion is filtered in two passes, because one isn't enough.** Explicit markers ("my take", "unpopular opinion", "thoughts?") are caught deterministically at collection, at every filter level including `off` — a company account's take is still a take. The rest is a judgement call, so the summarizer now returns `content_type: news | opinion` alongside `subject_type`, and the document generators drop the opinions. A keyword pass alone left @packyM's World Cup posts in the report; the LLM pass is what removes them.
- **Requiring the letters "AI" has one bad false negative**, and it is the most valuable item the report can carry: *"Sierra raised a $350M Series C led by Greenoaks"* never says AI. Items found by a Claude web search skip that test (`topic_assured`) because the query already fixed the topic; raw timeline posts still have to say it.
- **Reports link to the news, never to x.com.** Every post now carries `source_url` — the link it pointed at, with t.co resolved — and `x_url` for provenance. Headlines link to `source_url`; a post that links nowhere gets an unlinked headline rather than a link back to X. This also improved summaries: when there is a link, the *article* is fetched and summarized instead of the tweet, with a fallback to the post text when the page returns under 400 characters.
- **Dedup got the source link as a key.** Two accounts posting the same article are now provably the same story, so the pass collapses same-link, same-wording and same-story-different-wording, keeping the copy that has a link. The inline dedup snippets are gone; `tools/dedup_articles.py` and `news_filters.dedupe` are the one implementation.
- **`x_accounts.txt` was retargeted.** The lab accounts are commented out — every post they make is now dropped, so fetching them spent rate-limit budget to collect nothing — and startup/funding press accounts plus six startup-specific `search:` lines replaced them. The search lines matter disproportionately: a web search returns the article, so those items arrive with a real link, while a plain tweet usually links nowhere.

### 2026-08-12 Update — factual accuracy
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

   # Phase 3: Deduplication
   python tools/dedup_articles.py --inputs .tmp/raw_x.json \
     --output .tmp/classified_articles.json

   # Phase 3.5: Find + verify the article behind each link-less post
   python tools/resolve_sources.py --input .tmp/classified_articles.json --yes

   # Phase 4: Summarization with Claude (--curate skips paying for articles
   # the report would drop anyway; NEWS_MAX_WORKERS controls parallelism)
   python tools/summarize_articles.py --provider claude --yes --curate

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
