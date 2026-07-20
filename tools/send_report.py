#!/usr/bin/env python3
"""
Automated AI news report emailer.

Runs the full pipeline for the past 14 days then emails the generated
Word document to every address in EMAIL_RECIPIENTS.

Required .env variables:
  EMAIL_SENDER      Sender address  (e.g. reports@yourfirm.com)
  EMAIL_PASSWORD    SMTP password or Gmail App Password
  EMAIL_RECIPIENTS  Comma-separated recipient addresses

Optional .env variables:
  EMAIL_SMTP_HOST   SMTP server  (default: smtp.gmail.com)
  EMAIL_SMTP_PORT   SMTP port    (default: 587)

Railway Cron setup:
  Command:  python tools/send_report.py
  Schedule: 0 9 1,15 * *   (9 AM on the 1st and 15th of each month)

Gmail users: enable 2FA then create an App Password at
  https://myaccount.google.com/apppasswords
Use the App Password as EMAIL_PASSWORD — NOT your normal Gmail password.
"""

import json
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

TOOLS_DIR = Path(__file__).parent
BASE_DIR = TOOLS_DIR.parent
TMP_DIR = BASE_DIR / ".tmp"
OUTPUT_DIR = BASE_DIR / "output"


def _run(cmd: list, label: str):
    print(f"\n--- {label} ---")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {result.returncode})")


def run_pipeline(start_date: str, end_date: str) -> Path:
    """Run the full collection → summarise → generate pipeline.

    Twitter/X (via Nitter RSS) is the only collection source — no TechCrunch/TLDR.
    Summarization runs on the local Ollama model — no Claude/Anthropic API calls.
    """
    py = sys.executable
    tmp = TMP_DIR / "scheduled"
    out = OUTPUT_DIR / "scheduled"
    tmp.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    x_out = str(tmp / "raw_x.json")
    accounts_file = str(BASE_DIR / "x_accounts.txt")
    _run([
        py, str(TOOLS_DIR / "collect_x.py"),
        "--start_date", start_date, "--end_date", end_date,
        "--accounts", accounts_file,
        "--output", x_out,
    ], "Collecting X/Twitter")

    # Merge and deduplicate collected articles
    from tools.utils import deduplicate_articles
    all_articles = []
    p = Path(x_out)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            all_articles.extend(json.load(f))

    unique = deduplicate_articles(all_articles)
    print(f"\nDeduped: {len(unique)} unique posts from {len(all_articles)} total")
    if not unique:
        raise RuntimeError(
            "No posts collected — Nitter instances may be down/rate-limited. "
            "Check x_accounts.txt and NITTER_INSTANCES, or re-run later."
        )

    classified = str(tmp / "classified_articles.json")
    with open(classified, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)

    summarized = str(tmp / "summarized_articles.json")
    _run([
        py, str(TOOLS_DIR / "summarize_articles.py"),
        "--input", classified, "--output", summarized,
        "--provider", "ollama", "--yes", "--language", "zh",
    ], "Summarising with local Ollama")

    _run([
        py, str(TOOLS_DIR / "generate_ai_doc.py"),
        "--start_date", start_date, "--end_date", end_date,
        "--articles", summarized,
        "--output_dir", str(out),
        "--output-prefix", "AI_News",
        "--chinese-only",
    ], "Generating Word document")

    # Find the generated file
    pattern = f"AI_News_{start_date.replace('-','')}_{end_date.replace('-','')}.docx"
    doc_path = out / pattern
    if not doc_path.exists():
        candidates = sorted(out.glob("AI_News_*.docx"),
                            key=lambda f: f.stat().st_mtime, reverse=True)
        if not candidates:
            raise FileNotFoundError("No .docx file found after generation")
        doc_path = candidates[0]

    return doc_path


def send_email(doc_path: Path, start_date: str, end_date: str):
    """Attach the report and send to all recipients."""
    sender = os.getenv("EMAIL_SENDER", "").strip()
    password = os.getenv("EMAIL_PASSWORD", "").strip()
    recipients_raw = os.getenv("EMAIL_RECIPIENTS", "").strip()
    smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))

    if not sender or not password:
        raise ValueError("EMAIL_SENDER and EMAIL_PASSWORD must be set in .env")

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    if not recipients:
        raise ValueError("EMAIL_RECIPIENTS must be set in .env")

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"AI 资讯报告｜{start_date} 至 {end_date}"

    body = (
        f"各位好，\n\n"
        f"请查收本期 AI 行业双周报（{start_date} 至 {end_date}）。\n\n"
        f"报告包含：\n"
        f"  • 本周执行摘要\n"
        f"  • AI 新闻摘要（按公司分组）\n"
        f"  • AI 融资动态\n\n"
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(doc_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f'attachment; filename="{doc_path.name}"')
    msg.attach(part)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())

    print(f"\n✓ Report emailed to: {', '.join(recipients)}")


def main():
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    print(f"AI News Report: {start_date} → {end_date}")

    doc_path = run_pipeline(start_date, end_date)
    print(f"\n✓ Document ready: {doc_path}")

    send_email(doc_path, start_date, end_date)
    print("\n✓ Done.")


if __name__ == "__main__":
    main()
