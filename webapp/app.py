#!/usr/bin/env python3
"""
INCE News Dashboard — Flask web app

4 pages:
  /ai_news   — X/Twitter (+ optional WeChat), grouped table (OpenAI/Anthropic/BigTech/Other)
  /deeptech  — WeChat only, flat table + online funding search
  /consumer  — WeChat only, consumer format (行业动态 / 融资新闻)
  /crypto    — RootData fundraising scraper, Excel output

Run:
  python webapp/app.py
  # then open http://localhost:5001 in your browser
"""

import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
    stream_with_context,
)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent          # project root
TOOLS_DIR = BASE_DIR / "tools"
OUTPUT_DIR = BASE_DIR / "output"
TMP_DIR = BASE_DIR / ".tmp"

from ince_auth import install_auth

app = Flask(__name__)

# Shared-secret auth in front of every route. This service bound loopback,
# which was the only thing protecting it; remote access removes that.
install_auth(app, realm="News Workflow")

# ── In-memory job store ────────────────────────────────────────────────────────
# job_id -> {
#   status:      running | stopped | done | error
#   queue:       Queue for SSE log lines
#   stop_event:  threading.Event — set when user clicks Stop
#   proc:        current subprocess.Popen | None
#   proc_lock:   threading.Lock guarding `proc`
#   output_file: str | None
#   error:       str | None
# }
jobs: dict = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _default_dates():
    end = datetime.today()
    start = end - timedelta(days=14)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _log(job_id: str, msg: str):
    if job_id in jobs:
        jobs[job_id]["queue"].put(msg)


def _is_stopped(job_id: str) -> bool:
    return jobs[job_id]["stop_event"].is_set()


def _run_cmd(job_id: str, cmd: list, cwd: str = None) -> int:
    """
    Run a subprocess, stream stdout/stderr to the SSE queue.
    Returns the exit code, or -1 if the job was stopped mid-run.
    """
    if _is_stopped(job_id):
        return -1

    cwd = cwd or str(BASE_DIR)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=env,
    )

    # Register the process so /stop can kill it
    with jobs[job_id]["proc_lock"]:
        jobs[job_id]["proc"] = proc

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            _log(job_id, line)
        if _is_stopped(job_id):
            proc.terminate()
            break

    proc.wait()

    with jobs[job_id]["proc_lock"]:
        jobs[job_id]["proc"] = None

    if _is_stopped(job_id):
        return -1
    return proc.returncode


def _save_wechat_urls(job_id: str, raw_text: str, tmp_dir: Path) -> Path | None:
    urls = [
        u.strip()
        for u in raw_text.splitlines()
        if u.strip() and not u.strip().startswith("#")
    ]
    if not urls:
        return None
    path = tmp_dir / "wechat_urls.txt"
    path.write_text("\n".join(urls), encoding="utf-8")
    _log(job_id, f"Saved {len(urls)} WeChat URLs")
    return path


def _find_output(job_id: str, out_dir: Path, start_date: str, end_date: str, prefix: str) -> str | None:
    s = start_date.replace("-", "")
    e = end_date.replace("-", "")
    expected = out_dir / f"{prefix}_{s}_{e}.docx"
    if expected.exists():
        return str(expected)
    docx_files = sorted(out_dir.glob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(docx_files[0]) if docx_files else None


def _new_job() -> tuple[str, dict]:
    job_id = str(uuid.uuid4())[:8]
    job = {
        "status": "running",
        "queue": queue.Queue(),
        "stop_event": threading.Event(),
        "proc": None,
        "proc_lock": threading.Lock(),
        "output_file": None,
        "error": None,
    }
    jobs[job_id] = job
    return job_id, job


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ai_news")
def ai_news():
    start, end = _default_dates()
    return render_template("ai_news.html", default_start=start, default_end=end)


@app.route("/deeptech")
def deeptech():
    start, end = _default_dates()
    return render_template("deeptech.html", default_start=start, default_end=end)


@app.route("/consumer")
def consumer():
    start, end = _default_dates()
    return render_template("consumer.html", default_start=start, default_end=end)


@app.route("/crypto")
def crypto():
    start, end = _default_dates()
    return render_template("crypto.html", default_start=start, default_end=end)


@app.route("/insights")
def insights():
    start, end = _default_dates()
    return render_template("insights.html", default_start=start, default_end=end)


# ── Run endpoints ──────────────────────────────────────────────────────────────

@app.route("/run/ai_news", methods=["POST"])
def run_ai_news():
    data = request.json or {}
    job_id, _ = _new_job()
    threading.Thread(
        target=_pipeline_ai_news,
        args=(job_id, data.get("start_date"), data.get("end_date"),
              data.get("wechat_urls", ""), data.get("language", "zh"),
              data.get("wechat_funding_urls", "")),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/run/deeptech", methods=["POST"])
def run_deeptech():
    data = request.json or {}
    job_id, _ = _new_job()
    threading.Thread(
        target=_pipeline_deeptech,
        args=(job_id, data.get("start_date"), data.get("end_date"),
              data.get("wechat_urls", "")),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/run/consumer", methods=["POST"])
def run_consumer():
    data = request.json or {}
    job_id, _ = _new_job()
    threading.Thread(
        target=_pipeline_consumer,
        args=(job_id, data.get("start_date"), data.get("end_date"),
              data.get("wechat_urls", "")),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/run/crypto", methods=["POST"])
def run_crypto():
    data = request.json or {}
    job_id, _ = _new_job()
    threading.Thread(
        target=_pipeline_crypto,
        args=(job_id, data.get("start_date"), data.get("end_date"),
              float(data.get("min_amount", 10))),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/run/insights", methods=["POST"])
def run_insights():
    data = request.json or {}
    job_id, _ = _new_job()
    threading.Thread(
        target=_pipeline_insights,
        args=(job_id, data.get("start_date"), data.get("end_date"),
              data.get("language", "en")),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# ── Stop endpoint ──────────────────────────────────────────────────────────────

@app.route("/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    job = jobs[job_id]
    if job["status"] != "running":
        return jsonify({"ok": True, "msg": "Job already finished"})

    job["stop_event"].set()
    job["status"] = "stopped"

    # Kill the subprocess currently running, if any
    with job["proc_lock"]:
        proc = job["proc"]
        if proc and proc.poll() is None:
            proc.terminate()

    _log(job_id, "⚠ Stopped by user.")
    return jsonify({"ok": True})


# ── SSE stream ─────────────────────────────────────────────────────────────────

@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        q = jobs[job_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=15)
            except queue.Empty:
                if jobs[job_id]["status"] != "running":
                    break
                yield ": keepalive\n\n"  # SSE comment keeps proxy from closing idle connection
                continue
            if msg is None:   # sentinel
                status = jobs[job_id]["status"]
                if status == "done":
                    final = "__DONE__"
                elif status == "stopped":
                    final = "__STOPPED__"
                else:
                    final = "__ERROR__"
                yield f"data: {final}\n\n"
                break
            safe = msg.replace("\n", " ").replace("\r", "")
            yield f"data: {safe}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download/<job_id>")
def download(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    job = jobs[job_id]
    if job["status"] != "done" or not job["output_file"]:
        return jsonify({"error": "File not ready"}), 400
    if not os.path.exists(job["output_file"]):
        return jsonify({"error": "File missing on disk"}), 404
    return send_file(
        job["output_file"],
        as_attachment=True,
        download_name=os.path.basename(job["output_file"]),
    )


# ── Pipeline: AI News ─────────────────────────────────────────────────────────

def _pipeline_ai_news(job_id, start_date, end_date, wechat_urls, language, wechat_funding_urls=""):
    try:
        tmp = TMP_DIR / job_id
        tmp.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / job_id
        out.mkdir(parents=True, exist_ok=True)
        py = sys.executable

        # Phase 1: X/Twitter (the only automated source — no Claude/Anthropic calls anywhere in this pipeline)
        _log(job_id, "=== [1/6] Collecting X/Twitter ===")
        x_out = str(tmp / "raw_x.json")
        accounts_file = str(BASE_DIR / "x_accounts.txt")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "collect_x.py"),
            "--start_date", start_date, "--end_date", end_date,
            "--accounts", accounts_file,
            "--output", x_out,
        ]) == -1: return

        # Phase 2: WeChat news articles (optional manual addition, not an automated source)
        wechat_articles = []
        if wechat_urls.strip():
            _log(job_id, "=== [2/6] Collecting WeChat news articles ===")
            urls_file = _save_wechat_urls(job_id, wechat_urls, tmp)
            if urls_file:
                wc_out = str(tmp / "raw_wechat.json")
                if _run_cmd(job_id, [
                    py, str(TOOLS_DIR / "collect_wechat.py"),
                    "--urls", str(urls_file), "--output", wc_out,
                ]) == -1: return
                if os.path.exists(wc_out):
                    with open(wc_out, encoding="utf-8") as f:
                        wechat_articles = json.load(f)
        else:
            _log(job_id, "=== [2/6] No WeChat news URLs — skipping ===")

        if _is_stopped(job_id): return

        # Phase 3: WeChat fundraising articles (optional)
        funding_wechat_summarized = None
        if wechat_funding_urls.strip():
            _log(job_id, "=== [3/6] Collecting WeChat fundraising articles ===")
            funding_urls_file = tmp / "wechat_funding_urls.txt"
            funding_urls = [
                u.strip() for u in wechat_funding_urls.splitlines()
                if u.strip() and not u.strip().startswith("#")
            ]
            funding_urls_file.write_text("\n".join(funding_urls), encoding="utf-8")
            _log(job_id, f"Saved {len(funding_urls)} fundraising WeChat URLs")

            wc_funding_raw = str(tmp / "raw_wechat_funding.json")
            if _run_cmd(job_id, [
                py, str(TOOLS_DIR / "collect_wechat.py"),
                "--urls", str(funding_urls_file), "--output", wc_funding_raw,
            ]) == -1: return

            if os.path.exists(wc_funding_raw):
                wc_funding_summarized = str(tmp / "summarized_wechat_funding.json")
                if _run_cmd(job_id, [
                    py, str(TOOLS_DIR / "summarize_articles.py"),
                    "--input", wc_funding_raw, "--output", wc_funding_summarized,
                    "--provider", "claude", "--yes",
                    "--language", "zh", "--skip-fetch",
                ]) == -1: return
                if os.path.exists(wc_funding_summarized):
                    funding_wechat_summarized = wc_funding_summarized
        else:
            _log(job_id, "=== [3/6] No WeChat fundraising URLs — skipping ===")

        if _is_stopped(job_id): return

        # Phase 4: Deduplicate
        _log(job_id, "=== [4/6] Deduplicating ===")
        sys.path.insert(0, str(BASE_DIR))
        from tools.utils import deduplicate_articles
        all_articles = []
        if os.path.exists(x_out):
            with open(x_out, encoding="utf-8") as f:
                all_articles.extend(json.load(f))
        all_articles.extend(wechat_articles)
        unique = deduplicate_articles(all_articles)
        classified = str(tmp / "classified_articles.json")
        with open(classified, "w", encoding="utf-8") as f:
            json.dump(unique, f, ensure_ascii=False, indent=2)
        _log(job_id, f"Deduped: {len(unique)} unique articles")
        if not unique:
            raise RuntimeError(
                "No articles collected — Nitter instances may be down/rate-limited, "
                "check x_accounts.txt / NITTER_INSTANCES, or re-run later"
            )

        # Phase 5: Summarise
        _log(job_id, "=== [5/6] Summarising with Claude ===")
        summarized = str(tmp / "summarized_articles.json")
        lang_args = ["--language", "zh"] if language == "zh" else []
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "summarize_articles.py"),
            "--input", classified, "--output", summarized,
            "--provider", "claude", "--yes",
        ] + lang_args) == -1: return

        # Phase 6: Generate grouped Word doc
        _log(job_id, "=== [6/6] Generating Word document ===")
        mode_args = (
            ["--chinese-only"] if language == "zh"
            else ["--translate"] if language == "both"
            else []
        )
        funding_args = ["--funding-wechat", funding_wechat_summarized] if funding_wechat_summarized else []
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "generate_ai_doc.py"),
            "--start_date", start_date, "--end_date", end_date,
            "--articles", summarized,
            "--output_dir", str(out),
            "--output-prefix", "AI_News",
        ] + mode_args + funding_args) == -1: return

        output_file = _find_output(job_id, out, start_date, end_date, "AI_News")
        if output_file:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_file"] = output_file
            _log(job_id, "✓ Done! File ready for download.")
        else:
            raise RuntimeError("Output .docx not found after generation")

    except Exception as exc:
        if jobs[job_id]["status"] == "running":
            jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)
        _log(job_id, f"ERROR: {exc}")
    finally:
        jobs[job_id]["queue"].put(None)


# ── Pipeline: Deeptech ────────────────────────────────────────────────────────

def _pipeline_deeptech(job_id, start_date, end_date, wechat_urls):
    try:
        tmp = TMP_DIR / job_id
        tmp.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / job_id
        out.mkdir(parents=True, exist_ok=True)
        py = sys.executable

        if not wechat_urls.strip():
            raise ValueError("No WeChat URLs provided. Please paste at least one URL.")

        # Phase 1: Collect WeChat
        _log(job_id, "=== [1/4] Collecting WeChat articles ===")
        urls_file = _save_wechat_urls(job_id, wechat_urls, tmp)
        wc_out = str(tmp / "raw_wechat.json")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "collect_wechat.py"),
            "--urls", str(urls_file), "--output", wc_out,
        ]) == -1: return

        # Phase 2: Summarise in Chinese
        _log(job_id, "=== [2/4] Summarising with Claude ===")
        summarized = str(tmp / "summarized_wechat.json")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "summarize_articles.py"),
            "--input", wc_out, "--output", summarized,
            "--provider", "claude", "--yes",
            "--language", "zh", "--skip-fetch",
        ]) == -1: return

        if _is_stopped(job_id): return

        # Phase 3: Generate flat Word doc with deeptech funding section
        _log(job_id, "=== [3/4] Generating Word document ===")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "generate_word_doc.py"),
            "--start_date", start_date, "--end_date", end_date,
            "--articles", summarized,
            "--output_dir", str(out),
            "--chinese-only",
            "--output-prefix", "Deeptech_News",
            "--funding-topic", "deeptech",
            "--doc-title", "深科技新闻报告",
        ]) == -1: return

        # Phase 4: done
        output_file = _find_output(job_id, out, start_date, end_date, "Deeptech_News")
        if output_file:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_file"] = output_file
            _log(job_id, "✓ Done! File ready for download.")
        else:
            raise RuntimeError("Output .docx not found after generation")

    except Exception as exc:
        if jobs[job_id]["status"] == "running":
            jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)
        _log(job_id, f"ERROR: {exc}")
    finally:
        jobs[job_id]["queue"].put(None)


# ── Pipeline: Consumer ────────────────────────────────────────────────────────

def _pipeline_consumer(job_id, start_date, end_date, wechat_urls):
    try:
        tmp = TMP_DIR / job_id
        tmp.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / job_id
        out.mkdir(parents=True, exist_ok=True)
        py = sys.executable

        if not wechat_urls.strip():
            raise ValueError("No WeChat URLs provided. Please paste at least one URL.")

        # Phase 1: Collect WeChat
        _log(job_id, "=== [1/4] Collecting WeChat articles ===")
        urls_file = _save_wechat_urls(job_id, wechat_urls, tmp)
        wc_out = str(tmp / "raw_wechat.json")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "collect_wechat.py"),
            "--urls", str(urls_file), "--output", wc_out,
        ]) == -1: return

        # Phase 2: Deduplicate
        _log(job_id, "=== [2/4] Deduplicating ===")
        with open(wc_out, encoding="utf-8") as f:
            articles = json.load(f)
        seen, unique = set(), []
        for a in articles:
            url = a.get("url", "")
            if url and url not in seen:
                seen.add(url); unique.append(a)
        classified = str(tmp / "classified_wechat.json")
        with open(classified, "w", encoding="utf-8") as f:
            json.dump(unique, f, ensure_ascii=False, indent=2)
        _log(job_id, f"Deduped: {len(unique)} articles")

        if _is_stopped(job_id): return

        # Phase 3: Summarise (consumer mode)
        _log(job_id, "=== [3/4] Summarising with Claude (consumer mode) ===")
        summarized = str(tmp / "summarized_wechat.json")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "summarize_articles.py"),
            "--input", classified, "--output", summarized,
            "--provider", "claude", "--yes",
            "--language", "zh", "--consumer",
        ]) == -1: return

        # Phase 4: Generate consumer Word doc
        _log(job_id, "=== [4/4] Generating Word document ===")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "generate_consumer_doc.py"),
            "--start_date", start_date, "--end_date", end_date,
            "--articles", summarized,
            "--output_dir", str(out),
        ]) == -1: return

        output_file = _find_output(job_id, out, start_date, end_date, "Consumer_News")
        if output_file:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_file"] = output_file
            _log(job_id, "✓ Done! File ready for download.")
        else:
            raise RuntimeError("Output .docx not found after generation")

    except Exception as exc:
        if jobs[job_id]["status"] == "running":
            jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)
        _log(job_id, f"ERROR: {exc}")
    finally:
        jobs[job_id]["queue"].put(None)


# ── Pipeline: Crypto ─────────────────────────────────────────────────────────

def _pipeline_crypto(job_id, start_date, end_date, min_amount):
    try:
        tmp = TMP_DIR / job_id
        tmp.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / job_id
        out.mkdir(parents=True, exist_ok=True)
        py = sys.executable

        # Phase 1: Scrape RootData
        _log(job_id, "=== [1/3] Scraping RootData fundraising data ===")
        raw_out = tmp / "raw_rootdata.json"
        rc = _run_cmd(job_id, [
            py, str(TOOLS_DIR / "collect_rootdata.py"),
            "--start_date", start_date,
            "--end_date", end_date,
            "--min_amount", str(min_amount),
            "--output", str(raw_out),
        ])
        if rc == -1: return
        if not raw_out.exists():
            raise RuntimeError("collect_rootdata.py produced no output — check Chrome/Selenium and that RootData is accessible")

        # Phase 2: Summarise with Claude
        _log(job_id, "=== [2/3] Generating Info summaries with Claude ===")
        summarized = tmp / "summarized_rootdata.json"
        rc = _run_cmd(job_id, [
            py, str(TOOLS_DIR / "summarize_rootdata.py"),
            "--input", str(raw_out),
            "--output", str(summarized),
            "--yes",
        ])
        if rc == -1: return
        if not summarized.exists():
            raise RuntimeError(
                "summarize_rootdata.py produced no output — check ANTHROPIC_API_KEY."
            )

        if _is_stopped(job_id): return

        # Phase 3: Generate Excel
        _log(job_id, "=== [3/3] Generating Excel spreadsheet ===")
        rc = _run_cmd(job_id, [
            py, str(TOOLS_DIR / "generate_crypto_sheet.py"),
            "--input", str(summarized),
            "--start_date", start_date,
            "--end_date", end_date,
            "--output_dir", str(out),
        ])
        if rc == -1: return
        if rc != 0:
            raise RuntimeError(f"generate_crypto_sheet.py failed with exit code {rc}")

        # Locate output file
        s = start_date.replace("-", "")
        e = end_date.replace("-", "")
        expected = out / f"Crypto_News_{s}_{e}.xlsx"
        if expected.exists():
            output_file = str(expected)
        else:
            xlsx_files = sorted(out.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
            output_file = str(xlsx_files[0]) if xlsx_files else None

        if output_file:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_file"] = output_file
            _log(job_id, "✓ Done! File ready for download.")
        else:
            raise RuntimeError("Output .xlsx not found after generation")

    except Exception as exc:
        if jobs[job_id]["status"] == "running":
            jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)
        _log(job_id, f"ERROR: {exc}")
    finally:
        jobs[job_id]["queue"].put(None)


def _pipeline_insights(job_id, start_date, end_date, language="en"):
    try:
        tmp = TMP_DIR / job_id
        tmp.mkdir(parents=True, exist_ok=True)
        out = OUTPUT_DIR / job_id
        out.mkdir(parents=True, exist_ok=True)
        py = sys.executable

        # Phase 1: Collect TechCrunch
        _log(job_id, "=== [1/7] Collecting TechCrunch ===")
        tc_out = tmp / "raw_techcrunch.json"
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "collect_techcrunch.py"),
            "--start_date", start_date, "--end_date", end_date,
            "--output", str(tc_out),
        ]) == -1: return

        # Phase 2: Collect TLDR
        _log(job_id, "=== [2/7] Collecting TLDR newsletters ===")
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "collect_tldr.py"),
            "--start_date", start_date, "--end_date", end_date,
            "--output_dir", str(tmp),
        ]) == -1: return

        # Phase 3: Deduplicate
        _log(job_id, "=== [3/7] Deduplicating articles ===")
        classified = tmp / "classified_articles.json"
        dedup_script = f"""
import json, pathlib
files = ['{tmp}/raw_techcrunch.json', '{tmp}/raw_tldr_ai.json', '{tmp}/raw_tldr_main.json']
all_a = []
for f in files:
    p = pathlib.Path(f)
    if p.exists():
        all_a += json.loads(p.read_text())
seen, unique = set(), []
for a in all_a:
    url = a.get('url', '')
    if url and url not in seen:
        seen.add(url); unique.append(a)
pathlib.Path('{classified}').write_text(json.dumps(unique, ensure_ascii=False, indent=2))
print(f'Deduped: {{len(unique)}} articles')
"""
        if _run_cmd(job_id, [py, "-c", dedup_script]) == -1: return

        # Phase 4: Summarize (Claude — just pre-processing input for the
        # Claude-based insight/debate steps below, which are unchanged)
        _log(job_id, "=== [4/7] Summarizing articles with Claude ===")
        summarized = tmp / "summarized_articles.json"
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "summarize_articles.py"),
            "--provider", "claude",
            "--input", str(classified),
            "--output", str(summarized),
            "--yes",
        ]) == -1: return

        # Phase 5: Generate insights
        _log(job_id, "=== [5/7] Generating investment insights ===")
        insights_out = tmp / "insights.json"
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "generate_insights.py"),
            "--input", str(summarized),
            "--output", str(insights_out),
        ]) == -1: return

        # Phase 6: Multi-agent debate
        _log(job_id, "=== [6/7] Running bull / bear / partner debate ===")
        memos_out = tmp / "debate_memos.json"
        if _run_cmd(job_id, [
            py, str(TOOLS_DIR / "debate_agent.py"),
            "--input", str(insights_out),
            "--output", str(memos_out),
        ]) == -1: return

        # Phase 7: Deal sourcing + generate doc
        _log(job_id, "=== [7/7] Sourcing deals + generating report ===")
        deals_out = tmp / "deal_sourcing.json"
        _run_cmd(job_id, [
            py, str(TOOLS_DIR / "source_deals.py"),
            "--input", str(memos_out),
            "--output", str(deals_out),
        ])  # non-fatal if this fails

        rc = _run_cmd(job_id, [
            py, str(TOOLS_DIR / "generate_insight_doc.py"),
            "--memos", str(memos_out),
            "--deals", str(deals_out),
            "--start_date", start_date,
            "--end_date", end_date,
            "--output_dir", str(out),
            "--language", language,
        ])
        if rc == -1: return
        if rc != 0:
            raise RuntimeError("generate_insight_doc.py failed")

        s = start_date.replace("-", "")
        e = end_date.replace("-", "")
        lang_suffix = "_ZH" if language == "zh" else ""
        expected = out / f"Investment_Intelligence_{s}_{e}{lang_suffix}.docx"
        docx_files = sorted(out.glob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
        output_file = str(expected) if expected.exists() else (str(docx_files[0]) if docx_files else None)

        if output_file:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_file"] = output_file
            _log(job_id, "✓ Done! Investment Intelligence report ready.")
        else:
            raise RuntimeError("Output .docx not found after generation")

    except Exception as exc:
        if jobs[job_id]["status"] == "running":
            jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(exc)
        _log(job_id, f"ERROR: {exc}")
    finally:
        jobs[job_id]["queue"].put(None)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUT_DIR.mkdir(exist_ok=True)
    TMP_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", 5001))
    # Loopback only: this dashboard has no login, so a 0.0.0.0 bind handed it
    # to every device on whatever network the laptop was joined to. Tunnel
    # (ssh -L) if it genuinely needs to be reached from elsewhere.
    print(f"Starting INCE News Dashboard on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
