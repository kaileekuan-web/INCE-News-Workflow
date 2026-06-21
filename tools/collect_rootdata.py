#!/usr/bin/env python3
"""
Collect crypto fundraising deals from RootData.

Strategy:
  1. Open https://www.rootdata.com/Fundraising with headless Selenium
  2. Apply Amount filter (min/max inputs) and Date filter via the built-in filter panel
  3. Paginate through the filtered results (typically 1-5 pages for a 2-week window)
  4. Extract each row: date, company, company_url, round, amount_raw, amount_usd, investors, source_url

Input:  CLI args
Output: .tmp/raw_rootdata.json
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    print("ERROR: python-dateutil not installed. Run: pip install python-dateutil")
    sys.exit(1)

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException,
        StaleElementReferenceException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    print("ERROR: selenium not installed. Run: pip install selenium")
    sys.exit(1)

BASE_URL = "https://www.rootdata.com/Fundraising"
MAX_PAGES = 5
PAGE_DELAY = 1.5          # seconds between page navigations
FILTER_WAIT = 8           # max seconds to wait for table to reload after filters


# ── Amount parsing ──────────────────────────────────────────────────────────────

def parse_amount_usd(s: str) -> float | None:
    """
    Convert amount strings to float millions:
      "$ 550 M"   → 550.0
      "$ 1.2 B"   → 1200.0
      "€ 4 M"     → 4.0   (non-USD kept, caller decides)
      "--" / ""   → None
    Returns None for unknown/missing values.
    """
    if not s:
        return None
    s = s.strip()
    if s in ("--", "-", "N/A", "TBD", ""):
        return None

    # Remove currency symbols but keep the number and suffix
    cleaned = re.sub(r"[€£¥₩]", "", s)      # strip non-USD symbols
    cleaned = cleaned.replace("$", "").strip()

    # Handle range like "$10M - $20M" → take first number
    cleaned = re.split(r"\s*[-–]\s*", cleaned)[0].strip()

    m = re.match(r"([\d,]+\.?\d*)\s*([KMBkmb]?)", cleaned)
    if not m:
        return None

    val = float(m.group(1).replace(",", ""))
    suffix = m.group(2).upper()
    if suffix == "B":
        return val * 1000.0
    if suffix == "K":
        return val / 1000.0
    return val   # already in millions (M or no suffix)


# ── Date parsing ────────────────────────────────────────────────────────────────

def parse_date(text: str) -> str:
    """
    Parse display dates like "Apr 17" or "2026-04-17" → "YYYY-MM-DD".
    Infers current year; if parsed month > current month, assumes previous year.
    """
    text = text.strip()
    if not text or text == "--":
        return ""
    try:
        now = datetime.now()
        # Supply default year so "Apr 17" parses correctly
        dt = dateutil_parser.parse(text, default=datetime(now.year, 1, 1))
        # If the parsed date is in the future by more than 1 day, it's probably last year
        if dt > datetime.now() + __import__("datetime").timedelta(days=1):
            dt = dt.replace(year=dt.year - 1)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return text


# ── Selenium helpers ────────────────────────────────────────────────────────────

def _make_driver() -> webdriver.Chrome:
    chrome_bin = os.environ.get("CHROME_BIN")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")

    # Prefer undetected-chromedriver — bypasses Cloudflare/bot detection that blocks headless Selenium
    try:
        import subprocess as _sp
        import undetected_chromedriver as uc  # pip install undetected-chromedriver
        opts = uc.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        if chrome_bin:
            opts.binary_location = chrome_bin

        # Detect Chromium major version so uc doesn't guess wrong
        version_main = None
        try:
            bin_path = chrome_bin or "chromium"
            out = _sp.check_output([bin_path, "--version"], text=True, stderr=_sp.DEVNULL)
            version_main = int(out.strip().split()[-1].split(".")[0])
            print(f"  Detected Chromium version: {version_main}")
        except Exception:
            pass

        kwargs = {"options": opts, "use_subprocess": True}
        if chromedriver_path:
            kwargs["driver_executable_path"] = chromedriver_path
        if version_main:
            kwargs["version_main"] = version_main
        print("  Using undetected-chromedriver (bot-detection bypass)")
        return uc.Chrome(**kwargs)
    except ImportError:
        print("  WARNING: undetected-chromedriver not installed — RootData may block the request.")
        print("  Fix: pip install undetected-chromedriver")

    # Fallback: regular Selenium with stealth patches
    from selenium.webdriver.chrome.service import Service
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    if chrome_bin:
        options.binary_location = chrome_bin

    driver = (
        webdriver.Chrome(service=Service(chromedriver_path), options=options)
        if chromedriver_path
        else webdriver.Chrome(options=options)
    )
    # Patch navigator.webdriver to hide automation flag
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def _strip_avatar(text: str) -> str:
    """
    Clean RootData name cells that contain avatar + name [+ ticker] separated by newlines.
    'C\nCharms\nCHARMS' → 'Charms'   (strip 1-2 char avatar, keep first real word, drop ticker)
    'G\nGVFL'           → 'GVFL'
    'OpenTrade'         → 'OpenTrade'
    """
    text = text.strip()
    while "\n" in text:
        before, after = text.split("\n", 1)
        before, after = before.strip(), after.strip()
        if len(before) <= 2:
            text = after      # avatar prefix — discard and continue
        else:
            text = before     # real name found — stop (discard ticker on remaining lines)
            break
    return text


def _wait_for_rows(driver: webdriver.Chrome, timeout: int = 20) -> list:
    """Wait until the fundraising table rows are visible and return them."""
    selectors = [
        "table tbody tr",
        "tbody tr",
        "[class*='table'] [class*='row']",
        "[class*='list'] [class*='item']",
    ]
    # Divide total timeout across selectors so we don't wait 20s × 4 = 80s
    per_sel = max(3, timeout // len(selectors))
    for sel in selectors:
        try:
            WebDriverWait(driver, per_sel).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            rows = driver.find_elements(By.CSS_SELECTOR, sel)
            if rows:
                return rows
        except TimeoutException:
            continue
    return []


def _safe_text(element) -> str:
    try:
        return element.text.strip()
    except Exception:
        return ""


def _safe_attr(element, attr: str) -> str:
    try:
        return element.get_attribute(attr) or ""
    except Exception:
        return ""


# ── Filter interaction ──────────────────────────────────────────────────────────

def apply_filters(driver: webdriver.Chrome, start_date: str, end_date: str,
                  min_amount: float) -> bool:
    """
    Apply amount and date filters using RootData's built-in filter panel.
    Returns True on success, False if filter UI could not be interacted with
    (caller falls back to post-scrape filtering).
    """
    try:
        # ── Amount filter ─────────────────────────────────────────────────────
        print(f"  Applying amount filter: min=${min_amount}M, max=$100000M...")

        # Locate min/max amount inputs — try multiple selector strategies
        amount_inputs = []

        # Strategy 1: placeholders
        for ph in ["Min", "min", "最小", "从"]:
            elems = driver.find_elements(By.CSS_SELECTOR, f"input[placeholder*='{ph}']")
            if elems:
                amount_inputs = elems[:2]
                break

        # Strategy 2: number inputs inside a section containing "Amount" text
        if not amount_inputs:
            amount_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='number']")

        if len(amount_inputs) >= 2:
            # Clear and fill min input
            min_input = amount_inputs[0]
            min_input.click()
            min_input.send_keys(Keys.CONTROL + "a")
            min_input.send_keys(Keys.DELETE)
            min_input.send_keys(str(int(min_amount)))
            time.sleep(0.3)

            # Clear and fill max input
            max_input = amount_inputs[1]
            max_input.click()
            max_input.send_keys(Keys.CONTROL + "a")
            max_input.send_keys(Keys.DELETE)
            max_input.send_keys("100000")
            time.sleep(0.3)

            # Click Amount "Confirm" button
            _click_confirm_button(driver, context_text=["Amount", "金额", "Confirm", "确认"])
            time.sleep(0.5)
            print("  ✓ Amount filter applied")
        else:
            print("  WARNING: Amount filter inputs not found — will post-filter by amount")

        # ── Date filter ───────────────────────────────────────────────────────
        print(f"  Applying date filter: {start_date} to {end_date}...")

        # Try to find date range inputs
        date_inputs = []
        for ph in ["Start", "start", "开始", "From", "from"]:
            elems = driver.find_elements(By.CSS_SELECTOR, f"input[placeholder*='{ph}']")
            if elems:
                date_inputs = elems[:2]
                break

        # Fallback: find all date-type inputs
        if not date_inputs:
            date_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='date']")

        if len(date_inputs) >= 2:
            start_input = date_inputs[0]
            start_input.click()
            start_input.send_keys(Keys.CONTROL + "a")
            start_input.send_keys(Keys.DELETE)
            start_input.send_keys(start_date)
            time.sleep(0.3)

            end_input = date_inputs[1]
            end_input.click()
            end_input.send_keys(Keys.CONTROL + "a")
            end_input.send_keys(Keys.DELETE)
            end_input.send_keys(end_date)
            time.sleep(0.3)

            _click_confirm_button(driver, context_text=["Date", "日期", "Confirm", "确认"])
            time.sleep(0.5)
            print("  ✓ Date filter applied")
        else:
            print("  WARNING: Date filter inputs not found — will post-filter by date")

        # Wait for table to reload — actively poll instead of sleeping blindly
        print("  Waiting for table to reload after filters...")
        deadline = time.time() + FILTER_WAIT
        while time.time() < deadline:
            rows = _wait_for_rows(driver, timeout=2)
            if rows:
                break
            time.sleep(0.5)
        print("  Table ready.")
        return True

    except Exception as e:
        print(f"  WARNING: Filter interaction failed: {e}")
        print("  Proceeding without filters — will post-filter locally")
        return False


def _click_confirm_button(driver: webdriver.Chrome, context_text: list[str]):
    """Click the first visible Confirm button on the page."""
    # Try by button text
    for text in context_text:
        buttons = driver.find_elements(
            By.XPATH, f"//button[contains(., '{text}')] | //span[contains(@class, 'confirm') and contains(., '{text}')]"
        )
        for btn in buttons:
            if btn.is_displayed():
                btn.click()
                return
    # Generic fallback: any element with class containing 'confirm'
    for btn in driver.find_elements(By.CSS_SELECTOR, "[class*='confirm'], [class*='Confirm']"):
        if btn.is_displayed():
            btn.click()
            return


# ── Row extraction ──────────────────────────────────────────────────────────────

def extract_row(row) -> dict | None:
    """
    Extract one fundraising row into a dict.
    Column order (0-indexed): 0=Project, 1=Round, 2=Amount, 3=Valuation, 4=Date, 5=Source, 6=Investors
    """
    try:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 6:
            return None

        # Company name + URL
        company = ""
        company_url = ""
        try:
            a = cells[0].find_element(By.TAG_NAME, "a")
            company = _strip_avatar(_safe_text(a))
            if len(company) <= 2:
                company = _strip_avatar(_safe_text(cells[0]))
            href = _safe_attr(a, "href")
            if href and href.startswith("http"):
                from urllib.parse import urlparse
                company_url = urlparse(href).path + ("?" + urlparse(href).query if urlparse(href).query else "")
            elif href and href.startswith("/"):
                company_url = href
        except NoSuchElementException:
            company = _strip_avatar(_safe_text(cells[0]))

        if not company:
            return None

        # Round type
        round_type = _safe_text(cells[1])

        # Amount
        amount_raw = _safe_text(cells[2])
        amount_usd = parse_amount_usd(amount_raw)

        # Date (col 4; col 3 is valuation which we skip)
        date_text = _safe_text(cells[4])
        date = parse_date(date_text)

        # Source URL
        source_url = ""
        try:
            source_a = cells[5].find_element(By.TAG_NAME, "a")
            href = _safe_attr(source_a, "href")
            if href and href.startswith("http"):
                source_url = href
        except NoSuchElementException:
            pass

        # Investors (col 6 if exists)
        investors = []
        if len(cells) > 6:
            inv_links = cells[6].find_elements(By.TAG_NAME, "a")
            if inv_links:
                for a in inv_links:
                    name = _strip_avatar(_safe_text(a))
                    if name and len(name) > 1:
                        investors.append(name)
            else:
                raw = _safe_text(cells[6])
                if raw and raw != "--":
                    investors = [
                        _strip_avatar(v) for v in raw.split(",")
                        if _strip_avatar(v) and len(_strip_avatar(v)) > 1
                    ]

        return {
            "date": date,
            "company": company,
            "company_url": company_url,
            "round": round_type,
            "amount_raw": amount_raw,
            "amount_usd": amount_usd,
            "investors": investors,
            "source_url": source_url,
        }

    except (StaleElementReferenceException, Exception):
        return None


def scrape_current_page(driver: webdriver.Chrome) -> list[dict]:
    """Extract all deal rows from the currently visible table page."""
    rows = _wait_for_rows(driver, timeout=15)
    deals = []
    for row in rows:
        deal = extract_row(row)
        if deal and deal["company"]:
            deals.append(deal)
    return deals


def go_to_next_page(driver: webdriver.Chrome) -> bool:
    """
    Click the 'next page' button. Returns False if no next page available.
    """
    # Common selectors for next-page controls
    next_selectors = [
        "[class*='next']:not([disabled])",
        "[aria-label='next page']",
        "[aria-label='Next page']",
        "button.next",
        ".pagination-next:not(.disabled)",
        "[class*='pagination'] button:last-child:not([disabled])",
    ]
    # Also try by icon arrow (Vuetify uses v-icon with 'mdi-chevron-right')
    icon_selectors = [
        "[class*='mdi-chevron-right']",
        "i.v-icon.mdi-chevron-right",
    ]

    for sel in next_selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed() and btn.is_enabled():
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(PAGE_DELAY)
                return True
        except NoSuchElementException:
            continue

    for sel in icon_selectors:
        try:
            icon = driver.find_element(By.CSS_SELECTOR, sel)
            # Click the parent button of the icon
            parent = icon.find_element(By.XPATH, "..")
            if parent.is_displayed() and parent.is_enabled():
                driver.execute_script("arguments[0].click();", parent)
                time.sleep(PAGE_DELAY)
                return True
        except NoSuchElementException:
            continue

    return False


# ── Main collection function ────────────────────────────────────────────────────

def collect_rootdata(
    start_date: str,
    end_date: str,
    min_amount: float = 10.0,
    output: str = ".tmp/raw_rootdata.json",
):
    """
    Main entry point. Launches Selenium, applies filters, paginates, writes JSON.
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    driver = _make_driver()
    all_deals: list[dict] = []
    filters_applied = False

    try:
        print(f"Opening {BASE_URL}...")
        driver.get(BASE_URL)
        print("  Waiting for page to render...")
        _wait_for_rows(driver, timeout=20)

        # Detect bot-block: Cloudflare challenge or empty page
        title = driver.title.lower()
        if "just a moment" in title or "attention required" in title or "cloudflare" in title:
            driver.quit()
            raise RuntimeError(
                "RootData returned a Cloudflare challenge page.\n"
                "Fix: pip install undetected-chromedriver  (if not already installed)"
            )

        # Apply filters
        filters_applied = apply_filters(driver, start_date, end_date, min_amount)

        # Paginate
        page = 1
        while page <= MAX_PAGES:
            print(f"  Scraping page {page}...")
            deals = scrape_current_page(driver)

            if not deals:
                print("  No rows found on this page — stopping.")
                break

            # If filters weren't applied, post-filter here
            if not filters_applied:
                deals = [
                    d for d in deals
                    if (
                        d["amount_usd"] is not None and d["amount_usd"] >= min_amount
                    ) and (
                        not d["date"] or start_dt <= datetime.strptime(d["date"], "%Y-%m-%d") <= end_dt
                    )
                ]
                # Early stop: if the earliest date on this page is before start_date, stop
                dated = [d for d in deals if d["date"]]
                if dated and min(d["date"] for d in dated) < start_date:
                    print(f"  Reached dates older than {start_date} — stopping.")
                    all_deals.extend(deals)
                    break

            all_deals.extend(deals)
            print(f"    Found {len(deals)} deals (total so far: {len(all_deals)})")

            has_next = go_to_next_page(driver)
            if not has_next:
                print("  No next page — done.")
                break

            page += 1

    finally:
        driver.quit()

    # Drop rows with no parseable amount
    all_deals = [d for d in all_deals if d.get("amount_usd") is not None]

    # Deduplicate by company + date
    seen = set()
    unique = []
    for d in all_deals:
        key = (d["company"].lower(), d["date"])
        if key not in seen:
            seen.add(key)
            unique.append(d)

    unique.sort(key=lambda x: x.get("date", ""))

    print(f"\n✓ Collected {len(unique)} unique deals")

    os.makedirs(os.path.dirname(output) if os.path.dirname(output) else ".", exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(unique, f, ensure_ascii=False, indent=2)

    print(f"✓ Saved to {output}")


def main():
    parser = argparse.ArgumentParser(description="Collect crypto fundraising deals from RootData")
    parser.add_argument("--start_date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end_date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--min_amount", type=float, default=10.0,
                        help="Minimum deal size in $M (default: 10)")
    parser.add_argument("--output", default=".tmp/raw_rootdata.json",
                        help="Output JSON file (default: .tmp/raw_rootdata.json)")
    args = parser.parse_args()

    collect_rootdata(args.start_date, args.end_date, args.min_amount, args.output)


if __name__ == "__main__":
    main()
