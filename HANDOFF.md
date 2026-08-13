# INCE News Automation System — Handoff Guide

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture & How It Works](#2-architecture--how-it-works)
3. [Component Breakdown](#3-component-breakdown)
4. [How to Use the Web App](#4-how-to-use-the-web-app)
5. [Setup & Installation](#5-setup--installation)
6. [API Keys — What They Are and How to Update Them](#6-api-keys--what-they-are-and-how-to-update-them)
7. [Google OAuth Setup (Gmail API)](#7-google-oauth-setup-gmail-api)
8. [Deployment on Railway](#8-deployment-on-railway)
9. [WeChat Article Collection](#9-wechat-article-collection)
10. [Troubleshooting Common Issues](#10-troubleshooting-common-issues)
11. [Updating Workflows](#11-updating-workflows)
12. [Known Issues & Areas for Improvement](#12-known-issues--areas-for-improvement)

---

## 1. Project Overview

This system automates the bi-weekly INCE news briefing. It collects articles from multiple sources, summarizes them using AI, and generates formatted Word documents and Excel spreadsheets that get distributed to the team.

It produces **4 types of reports**, each run on-demand through a web dashboard:

| Report | Output | Sources | Frequency |
|--------|--------|---------|-----------|
| **AI News** | Word doc (`.docx`) | **X/Twitter (primary)**, optional Wechat 公众号 | Bi-weekly |
| **Deeptech News** | Word doc (`.docx`) | Wechat 公众号 | Bi-weekly |
| **Consumer News** | Word doc (`.docx`) | Wechat 公众号 | Bi-weekly |
| **Crypto Fundraising** | Excel spreadsheet (`.xlsx`) | RootData.com | On-demand |

Each report has two sections: a news summary table and a fundraising activity table. The AI News and Deeptech reports can be bilingual (Chinese is preferred, Bilingual function needs fixing). Consumer News is Chinese-only (sourced from Chinese media).

---

## 2. Architecture & How It Works

### High-Level Data Flow

```
User fills form in web browser
         ↓
Flask web app (webapp/app.py)
  - Creates a job with unique ID
  - Starts the pipeline in a background thread
  - Streams live logs back to browser via SSE
         ↓
Pipeline runs in sequence:
  1. COLLECT  → fetches articles from APIs/web → saves to .tmp/
  2. PROCESS  → deduplicates, summarizes with AI → saves to .tmp/
  3. GENERATE → creates Word/Excel document → saves to output/
         ↓
User clicks "Download" → gets the file
```

### Key Concepts

**Background jobs + live streaming:** When you click "Run" in the web app, the pipeline runs in a background thread. The browser connects to a Server-Sent Events (SSE) endpoint that streams each log line in real time — you can watch what the script is doing without refreshing the page.

**Intermediate files:** Each step in the pipeline writes a `.json` file to `.tmp/`. If a step fails, you can inspect these files to understand what happened. They are temporary and safe to delete.

**AI models used:**
- **Claude** (`claude-opus-5`) — everything: article summarization, relevance scoring, Chinese translation, funding extraction, executive summaries, insights, and the fundraising web search (via Claude's server-side `web_search` tool). Override the model for a whole run with `NEWS_CLAUDE_MODEL` in `.env` (e.g. `claude-sonnet-5` to cut cost).

---

## 3. Component Breakdown

### `webapp/` — The Web Dashboard

```
webapp/
├── app.py              # Flask server — all routes, job management, SSE streaming
└── templates/
    ├── base.html       # Shared navigation and layout (Tailwind CSS)
    ├── index.html      # Home page
    ├── ai_news.html    # AI News report page
    ├── deeptech.html   # Deeptech report page
    ├── consumer.html   # Consumer News report page
    └── crypto.html     # Crypto report page
```

**`app.py`** handles:
- Route `/ai_news`, `/deeptech`, `/consumer`, `/crypto` — render the form pages
- Route `/run/<page>` (POST) — start a pipeline job, return a `job_id`
- Route `/stream/<job_id>` — SSE endpoint that streams log lines to the browser
- Route `/download/<job_id>` — serve the completed output file
- Route `/stop/<job_id>` — cancel a running job
- In-memory job store tracking status, log queue, subprocess reference

### `tools/` — Python Execution Scripts

Each tool is a standalone Python script that does one thing well.

**Data Collectors**

| Script | What It Does | Data Source | Output |
|--------|-------------|-------------|--------|
| `collect_x.py` | **Primary AI News source.** Pulls the accounts in `x_accounts.txt` | Nitter RSS mirrors, falling back to Claude web search per account | `.tmp/raw_x.json` |
| `collect_x_claude.py` | The fallback above; also runnable standalone | Claude web search | `.tmp/raw_x_claude.json` |
| `collect_techcrunch.py` | Fetches AI articles from TechCrunch (not used by the AI News pipeline) | NewsAPI.org (fallback: RSS) | `.tmp/raw_techcrunch.json` |
| `collect_tldr.py` | Reads TLDR AI and TLDR Main email newsletters (not used by the AI News pipeline) | Gmail API (your inbox) | `.tmp/raw_tldr_ai.json`, `.tmp/raw_tldr_main.json` |
| `collect_wechat.py` | Scrapes WeChat public account articles from a URL list | WeChat (direct web scrape) | `.tmp/raw_wechat.json` |
| `collect_rootdata.py` | Scrapes crypto fundraising deals | RootData.com (headless Chrome) | `.tmp/raw_rootdata.json` |
| `collect_substack.py` | Fetches Z Potentials newsletter via RSS | Substack | `.tmp/raw_substack.json` |

**Summarizers**

| Script | What It Does |
|--------|-------------|
| `summarize_articles.py` | Fetches full article content, generates AI summaries with Claude. `--language zh` for Chinese output. Summary length scales to source length and every figure is checked against the source. |
| `grounding.py` | Arithmetic grounding check — normalizes figures on both sides (so `$50 million` matches `5000万美元`) and reports any number in a summary the source doesn't support. Run `python tools/grounding.py` for its self-tests. |
| `summarize_rootdata.py` | Generates bilingual company descriptions for crypto deals using Claude. |

**Document Generators**

| Script | Output Format | Notes |
|--------|--------------|-------|
| `generate_word_doc.py` | `.docx` — AI News | 2-column table (Date \| Chinese+English summary), + fundraising table via ChatGPT web search |
| `generate_ai_doc.py` | `.docx` — AI News (grouped) | Articles grouped by company category (OpenAI / Anthropic / BigTech / Other) |
| `generate_consumer_doc.py` | `.docx` — Consumer News | Chinese-only, 消费科技新闻摘要 + 消费科技融资动态 |
| `generate_crypto_sheet.py` | `.xlsx` — Crypto | 6-column Excel with dark navy header, frozen row, hyperlinked company names |

**Utilities**

| Script | What It Does |
|--------|-------------|
| `utils.py` | Shared functions: date validation, text cleaning, deduplication |
| `detect_funding.py` | Keyword-based funding event classifier (not used in current main workflows) |
| `translate_content.py` | Standalone Claude translation utility for one-off backfills, not used by any pipeline (the pipelines summarize straight into Chinese instead). Delegates to `translate_to_chinese_claude` in `tools/utils.py`. |

### `workflows/` — Step-by-Step SOPs

Markdown files that document exactly how to run each pipeline, including commands, expected outputs, edge cases, and lessons learned. **Always read the relevant workflow before running a pipeline manually.**

| File | Covers |
|------|--------|
| `collect_ai_news.md` | AI News pipeline (X/Twitter + optional WeChat → Word doc) |
| `collect_consumer_news.md` | Consumer News pipeline (WeChat → Chinese Word doc) |

### Config & Credentials

| File | Purpose | In Git? |
|------|---------|---------|
| `.env` | All API keys and environment variables | No (gitignored) |
| `.env.example` | Template showing which keys are needed | Yes |
| `credentials.json` | Google OAuth client secret (from Google Cloud Console) | No (gitignored) |
| `token.json` | Gmail OAuth access token (auto-generated on first auth) | No (gitignored) |

---

## 4. How to Use the Web App

### Starting the App

There are three ways to access the app:

**Option 1: Currently deployed (temporary)**
https://ince-news-workflow-2026-production.up.railway.app/ 
The app is currently live on Railway under the previous intern's account. You can use it as-is until you set up your own deployment. Note that once they close their account or stop paying, the app will go offline — so set up your own deployment (Option 3) as soon as possible.

**Option 2: Run locally**
```bash
# From the project root, with your virtual environment activated:
python webapp/app.py
# Open http://localhost:5001 in your browser
```
This runs the full app on your own machine. Good for development and testing. First-time setup: clone the repo and install dependencies (see [Section 5 — Getting the Code](#5-setup--installation)). Requires all API keys in `.env` and Gmail OAuth set up locally (see Sections 6 and 7). The app will stop when you close your terminal.

**Option 3: Deploy your own Railway instance**
See [Section 8](#8-deployment-on-railway) for full instructions. This gives you a persistent public URL that anyone on the team can use. You'll need to create a free Railway account, connect the git repo, and copy all environment variables from the previous intern before their app goes offline.

The home page (`/`) lists the four report types. Click any to go to its page.

### Running a Report

Each page has a form. Fill it in and click **Run**.

**AI News (`/ai_news`)**
- Start date and end date (YYYY-MM-DD)
- WeChat article URLs (one per line, optional — used for the grouped table)
- Output: `output/AI_News_YYYYMMDD_YYYYMMDD.docx`
- Time: ~15–20 minutes | Cost: ~$1.00–2.00

**Deeptech (`/deeptech`)**
- Start date and end date
- WeChat article URLs (one per line)
- Output: `output/Deeptech_News_YYYYMMDD_YYYYMMDD.docx`
- Time: ~10–15 minutes | Cost: ~$0.50–1.00

**Consumer News (`/consumer`)**
- Start date and end date
- WeChat article URLs (one per line)
- Output: `output/Consumer_News_YYYYMMDD_YYYYMMDD.docx`
- Time: ~7–15 minutes | Cost: ~$0.35–0.75

**Crypto Fundraising (`/crypto`)**
- Start date and end date
- Min and max funding amount (USD millions)
- Output: `output/Crypto_News_YYYYMMDD_YYYYMMDD.xlsx`
- Time: ~5–10 minutes | Cost: minimal (no AI summarization by default)

### While the Job Runs

- Logs stream live in the browser — you can watch each step execute
- Click **Stop** to cancel the job at any point
- Do not close the tab or refresh — you will lose the log stream (the job continues running in the background but you won't be able to reconnect to it)

### Downloading the Output

When the job finishes, a **Download** button appears. Click it to save the `.docx` or `.xlsx` file.

---

## 5. Setup & Installation

### Prerequisites

- Python 3.11 or newer
- Google Chrome (required for the Crypto pipeline — used by Selenium)
- A terminal / command line

### Getting the Code

```bash
# Clone the repository
git clone https://github.com/ychen945/INCE-News-Workflow.git
cd INCE-News-Workflow
```

If you don't have git installed, download it from [git-scm.com](https://git-scm.com) first. You'll also need access to the GitHub repo — ask the previous intern or INCE team lead to add you as a collaborator.

### Local Setup

```bash
# 1. Enter the project folder (if you just cloned, you're already here)
cd INCE-News-Workflow

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

# 3. Install dependencies
#    Use requirements-local.txt on a dev machine: it adds the claim-envelope
#    package from the sibling INCE-Verification-Service checkout, which is kept
#    out of requirements.txt because pip cannot resolve a relative editable path
#    inside the Docker build (it fails the whole image build).
pip install -r requirements-local.txt   # containers use plain requirements.txt

# 4. Set up environment variables
cp .env.example .env
# Open .env in a text editor and fill in your API keys (see Section 6)

# 5. Set up Gmail OAuth (one-time — see Section 7)

# 6. Run the app
python webapp/app.py
# Open http://localhost:5001
```

### Running on a Server (Railway)

See [Section 8](#8-deployment-on-railway) for deployment instructions.

---

## 6. API Keys — What They Are and How to Update Them

All API keys live in the `.env` file at the project root. **Never commit this file to git** — it is already in `.gitignore`.

### Key Reference Table

| Variable in `.env` | Service | Free Tier? | Where to Get It | Used For |
|--------------------|---------|-----------|----------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic Claude | No | [console.anthropic.com](https://console.anthropic.com) → API Keys | **Required.** Every LLM call in every pipeline: summarization, scoring, translation, funding extraction, and the fundraising web search |
| `NEWS_CLAUDE_MODEL` | — | — | optional `.env` override | Model for the whole pipeline (default `claude-opus-5`). Also `NEWS_CLAUDE_EFFORT` (default `low`) and `NEWS_CLAUDE_MAX_TOKENS` (default `4096`) |
| `NEWSAPI_ORG_KEY` | NewsAPI.org | Yes (100 req/day) | [newsapi.org](https://newsapi.org) → Get API Key | TechCrunch article collection for AI News pipeline |

### How to Update Keys Locally

Open `.env` in any text editor and replace the value:

```
ANTHROPIC_API_KEY=your_new_key_here
```

No restart needed — each tool reads the `.env` file fresh when it runs.

### How to Update Keys on Railway (Production)

1. Go to [railway.app](https://railway.app) and open the INCE project
2. Click **Variables** in the left sidebar
3. Find the variable you want to update and click the pencil icon
4. Paste the new value and save
5. Railway will automatically redeploy with the new value

### Cost Guidance

These are approximate costs per report run:

| API | Cost Driver | Estimated Cost |
|-----|------------|----------------|
| Anthropic Claude | Summarization + translation + funding extraction | ~$0.03 per article at Opus 5 list rates |
| Anthropic Claude | Fundraising web search | One multi-search turn per day in the range, plus $10 per 1,000 searches |
| NewsAPI.org | Article fetching | Free (within 100 req/day) |

Running AI News + Consumer News bi-weekly costs roughly **$3–8/month** in API fees. If costs spike, check that you're not running test runs with large date ranges unnecessarily.

---

## 7. Google OAuth Setup (Gmail API)

### Why This Is Needed

The AI News pipeline collects TLDR newsletters (TLDR AI and TLDR Main) directly from Gmail. The script authenticates as a real Google account to search the inbox for these emails. This requires a **Google Cloud project** with the Gmail API enabled and OAuth credentials.

### Files Involved

| File | What It Is |
|------|-----------|
| `credentials.json` | OAuth client secret — downloaded from Google Cloud Console once |
| `token.json` | Access token — auto-generated the first time you authenticate |

### First-Time Setup (Local)

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or ask the previous intern to add you to the existing one)
3. Enable the **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable
4. Create OAuth credentials: APIs & Services → Credentials → Create Credentials → OAuth client ID → Desktop app
5. Download the credentials file and save it as `credentials.json` in the project root
6. Run the TLDR collector once — it will open a browser window asking you to log in:
   ```bash
   python tools/collect_tldr.py --start_date 2026-01-01 --end_date 2026-01-07
   ```
7. Log in with the Gmail account that receives TLDR newsletters and grant access
8. `token.json` is automatically created in the project root — **keep this file**

Future runs will use `token.json` automatically without prompting. The token refreshes itself when it expires.

### Setting Up for Railway (Production Server)

The Railway server cannot open a browser for OAuth. Instead, the token is passed as an environment variable:

1. After completing local setup, open `token.json` and copy its entire contents
2. Minify it to a single line (use [jsonformatter.org](https://jsonformatter.org) → "Minify")
3. In Railway dashboard → Variables, add a new variable:
   - Name: `GMAIL_TOKEN_JSON`
   - Value: paste the minified JSON string
4. The app will automatically use this environment variable instead of `token.json`

### Transferring to a New Google Account

If you need to use a different Gmail account (e.g., the new intern's account that receives TLDR):

1. In Google Cloud Console, add the new email as a test user under OAuth consent screen (or use the existing project)
2. Delete the existing `token.json` from the project root: `rm token.json`
3. Run the TLDR collector locally — it will prompt for re-authentication
4. Log in with the new Gmail account
5. A new `token.json` is created — update `GMAIL_TOKEN_JSON` in Railway with the new token

> **Note:** The Gmail account used must be subscribed to both TLDR AI and TLDR Main newsletters. The script searches the inbox for emails from `dan@tldrnewsletter.com`.

---

## 8. Deployment on Railway

[Railway](https://railway.app) is the cloud platform that hosts the web app so anyone can use it from a browser without running it locally.

### How It Works

The app is containerized with Docker. When you push code to the main git branch, Railway automatically rebuilds and redeploys.

- **`Dockerfile`** — defines the container (Python 3.11, Chrome/ChromeDriver for Selenium, all dependencies)
- **`railway.toml`** — tells Railway how to build and run the app:
  ```toml
  [build]
  dockerfilePath = "Dockerfile"
  
  [deploy]
  startCommand = "python webapp/app.py"
  healthcheckPath = "/"
  ```

### Deploying Changes

```bash
git add -A ':!credentials.json' ':!token.json' ':!.claude/'
git commit -m "describe your change"
git push origin main
```

> ⚠️ Those exclusions matter. `credentials.json`, `token.json` and
> `.claude/credentials.json` contain live Google OAuth secrets (including a
> refresh token). They are in `.gitignore` as of 2026-08-12, so a plain
> `git add .` is safe now — the explicit form above is belt-and-braces. If you
> ever see them appear in `git status` as staged, stop and unstage them:
> secrets in git history are permanent and require rotating the credentials.

Railway detects the push, builds the Docker image, and deploys it. The process takes 2–5 minutes. You can watch the build log in the Railway dashboard.

### Managing Environment Variables

All API keys and the `GMAIL_TOKEN_JSON` variable live in the Railway dashboard under **Variables**, not in any file. This keeps secrets out of the repository.

### Viewing Logs

In Railway dashboard → your service → **Logs** tab. You can see both build logs and runtime logs here if something goes wrong in production.

### Health Check

Railway pings `GET /` every 30 seconds. If the app crashes, Railway automatically restarts it (configured with `restartPolicyType = "on_failure"`).

---

## 9. WeChat Article Collection

WeChat collection is a **manual step** — there is no automatic way to discover new articles. You need to find and paste the article URLs yourself before running the Deeptech or Consumer News pipelines.

### Where to Find Articles

Subscribe to or bookmark these WeChat public accounts:

**AI News:** 
- 36氪
- Z potentials
- 量子位
- 新智元

**Deep Tech News:**
- **半导体：** 芯东西、集微网、深科技、半导体行业观察
- **机器人：** 机器人大讲堂、机器人前瞻、机器人行业观察
- **General：** 东四十条资本（每周有融资新闻summary）、创投日报
- **AR：** 微晶绘、VR陀螺
- **汽车/新能源：** 盖世汽车、新能源情报局

**Consumer News:** 
- 36氪未来消费
- 刀法研究所
- IPO早知道
- 极客公园
- AING硬迹
- Z Lives

Browse these accounts every two weeks, collect relevant article URLs, and paste them into the URL field in the web app (one per line).

### URL Format

WeChat article URLs look like:
```
https://mp.weixin.qq.com/s/aBcDeFgHiJkLmNoPqRsTuV
```

You can use `#` at the start of a line to add a comment (ignored by the script):
```
# Consumer News — 2026-03-10 to 2026-03-23
https://mp.weixin.qq.com/s/...
https://mp.weixin.qq.com/s/...
```

### Tips

- Paste URLs in the order you want them processed — the script fetches them sequentially
- Duplicate URLs are automatically skipped
- Some articles require WeChat app login and won't be publicly accessible — test in a browser first if you're unsure
- If an article can't be fetched, the script continues and uses the article title/URL as a fallback

---

## 10. Troubleshooting Common Issues

### Gmail authentication fails or expires

**Symptom:** Error mentioning `token.json`, `invalid_grant`, or `403 forbidden` during TLDR collection  
**Fix:**
```bash
rm token.json
python tools/collect_tldr.py --start_date 2026-01-01 --end_date 2026-01-07
# A browser window opens — log in again
```
Then update `GMAIL_TOKEN_JSON` in Railway with the new token contents.

---

### NewsAPI rate limit hit

**Symptom:** Log shows "rate limit exceeded" or 429 error from NewsAPI  
**Fix:** No action needed — the script automatically falls back to the TechCrunch RSS feed. The output will be similar quality. You'll see a note in the logs when this happens.  
**Prevention:** NewsAPI allows 100 requests/day on the free plan. Avoid running the AI News pipeline more than once per day.

---

### A summary contains a number that isn't in the source

**Symptom:** the summarization step ends with `⚠ GROUNDING: N article(s) contain figures not found in the source`, listing article titles and figures
**What it means:** the arithmetic check found a figure the source doesn't support, and the one repair pass didn't clear it. Those articles carry `grounding_flags` in `.tmp/summarized_articles.json`
**Fix:** check those articles before sending the report. A clean run prints `✓ Grounding: every figure in every summary traces to its source` instead

---

### Anthropic credit error (summaries or funding section empty)

**Symptom:** Log shows `WARNING: Claude call failed` mentioning credit balance, or summaries fall back to raw descriptions  
**Fix:** Top up at [console.anthropic.com](https://console.anthropic.com) → Billing. Re-run the pipeline — completed articles are skipped, so only the failed ones are retried.

---

### Selenium / Chrome error in Crypto pipeline

**Symptom:** Error mentioning ChromeDriver, `WebDriverException`, or "session not created"  
**Fix (local):** Ensure Google Chrome is installed. ChromeDriver version must match Chrome version — run `google-chrome --version` and check that `chromedriver --version` matches.  
**Fix (Railway):** The Dockerfile installs Chromium and ChromeDriver automatically. If this breaks, check for a Chrome/Chromium version mismatch in the Dockerfile.

---

### WeChat articles return empty content

**Symptom:** Summary is very short or just repeats the title; date column is blank  
**Cause:** Some WeChat articles require login to view, or WeChat changed their HTML structure  
**Fix:** Open the URL in a browser to confirm it's publicly accessible. If WeChat changed their selectors, update `collect_wechat.py`:
- Title: `<h1 class="rich_media_title">`
- Date: `<em id="publish_time">`
- Content: `<div id="js_content">`

---

### Job appears stuck / no logs appear

**Fix:** Click **Stop** and re-run. Check the terminal where you started `webapp/app.py` for any Python errors that didn't make it to the SSE stream.

---

### Paywall articles produce weak summaries

**Expected behavior.** Articles behind paywalls (WSJ, Bloomberg, NYT) fall back to using the article description rather than full content. The summary will be shorter but still useful. This affects ~10–20% of TechCrunch articles.

---

### Summaries appear in English when Chinese is expected

**Check:** For Consumer News, confirm the pipeline is passing `--language zh` to `summarize_articles.py`.  
**Check:** If the article content fetch failed, the script summarizes from the English description — Chinese output is not possible in this case.

---

## 11. Updating Workflows

The `workflows/` folder contains the detailed SOPs for each pipeline. **When you discover something new — a new rate limit, a changed HTML structure, a faster approach — update the workflow file.** This is how institutional knowledge gets preserved.

### When to Update a Workflow

- You hit an error that wasn't documented
- A workaround you found should be the new default approach
- A tool's behavior changed (API update, website redesign)
- You added a new pipeline step or changed the command

### How to Update

1. Open the relevant workflow file in `workflows/`
2. Add a dated entry under **Lessons Learned** describing what you found and what changed
3. Update the **Steps** section if the commands changed
4. Update **Edge Cases & Error Handling** with the new issue and fix
5. Commit the change: `git commit -m "Update AI News workflow: document new rate limit behavior"`

### Important Rule

Do not delete or overwrite existing workflow history. Add new entries rather than replacing old ones — the log of lessons learned is valuable.

---

## 12. Known Issues & Areas for Improvement

These are known problems or incomplete features that the next intern should be aware of and ideally pick up.

### Consumer News — Formatting Issues

The Consumer News Word doc output (`generate_consumer_doc.py`) has currently does not meet the formatting needs of the consumer team. Please confirm with Ivy Qiu on speicifc formatting requirements and implement the changes. 

### Bilingual Functionality Needs Fixing

The AI News reports are intended to display both Chinese and English content side by side (Chinese first, English below) for bilingual selection. The translation function currently struggles with consistent English and Chinese outcomes and will need additional work in that. 
---

## Quick Reference — Running Each Pipeline

### AI News (bi-weekly)
Requires: `ANTHROPIC_API_KEY` (X/Twitter collection needs no key)
```
Web app → /ai_news → enter dates → Run
Output:  output/AI_News_YYYYMMDD_YYYYMMDD.docx
Time:    ~15–20 min | Cost: ~$1.00–2.00
```

### Consumer News (bi-weekly)
Requires: `ANTHROPIC_API_KEY`, WeChat URLs
```
Web app → /consumer → enter dates + paste URLs → Run
Output:  output/Consumer_News_YYYYMMDD_YYYYMMDD.docx
Time:    ~7–15 min | Cost: ~$0.35–0.75
```

### Deeptech News (bi-weekly)
Requires: `ANTHROPIC_API_KEY`, WeChat URLs
```
Web app → /deeptech → enter dates + paste URLs → Run
Output:  output/Deeptech_News_YYYYMMDD_YYYYMMDD.docx
Time:    ~10–15 min | Cost: ~$0.50–1.00
```

### Crypto Fundraising (on-demand)
Requires: Chrome installed, no API keys needed
```
Web app → /crypto → enter dates + amount range → Run
Output:  output/Crypto_News_YYYYMMDD_YYYYMMDD.xlsx
Time:    ~5–10 min | Cost: minimal
```

---

*Last updated: June 2026*
