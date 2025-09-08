#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# --------------------
# Config via env vars
# --------------------
URL                 = os.getenv("MONITOR_URL", "").strip()
# One of the following must be provided:
CSS_SELECTOR        = os.getenv("MONITOR_CSS_SELECTOR", "").strip()      # e.g. "#availability .month"
REGEX_CAPTURE       = os.getenv("MONITOR_REGEX_CAPTURE", "").strip()      # e.g. r"Reservations\s+for\s+([A-Za-z]+)"
TIMEOUT_SEC         = int(os.getenv("MONITOR_TIMEOUT_SEC", "30"))

STATE_FILE          = Path(os.getenv("MONITOR_STATE_FILE", "./monitor_state.json"))
PDF_DIR             = Path(os.getenv("MONITOR_PDF_DIR", "./pdf_changes"))
LOG_DIR             = Path(os.getenv("MONITOR_LOG_DIR", "./logs"))
DAILY_SUMMARY_HHMM  = os.getenv("MONITOR_DAILY_SUMMARY_HHMM", "20:00")    # used only if you run as a daemon

SMTP_HOST           = os.getenv("SMTP_HOST", "")
SMTP_PORT           = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER           = os.getenv("SMTP_USER", "")
SMTP_PASS           = os.getenv("SMTP_PASS", "")
MAIL_FROM           = os.getenv("MAIL_FROM", SMTP_USER or "")
MAIL_TO             = os.getenv("MAIL_TO", "")
MAIL_SUBJECT_PREFIX = os.getenv("MAIL_SUBJECT_PREFIX", "[Monitor]")

# --------------------
# Utilities
# --------------------
def now_utc():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def today_key():
    # YYYY-MM-DD (UTC) for daily summaries
    return now_utc().date().isoformat()

def ensure_dirs():
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_value": None,
        "last_value_hash": None,
        "last_change_ts": None,
        "changes_today": False,
        "last_summary_day": None
    }

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def fetch_content(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AO-Monitor/1.0)"
    }
    r = requests.get(url, headers=headers, timeout=TIMEOUT_SEC)
    r.raise_for_status()
    return r.text

def extract_value(html: str) -> str:
    """
    Extract the single 'word/value' we care about, using either CSS selector text
    or a regex capture group (group 1). Priority: CSS_SELECTOR, then REGEX_CAPTURE.
    """
    if CSS_SELECTOR:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one(CSS_SELECTOR)
        if not node:
            raise ValueError(f"CSS selector not found: {CSS_SELECTOR}")
        value = node.get_text(strip=True)
        if not value:
            raise ValueError(f"CSS selector found but empty text: {CSS_SELECTOR}")
        return value

    if REGEX_CAPTURE:
        m = re.search(REGEX_CAPTURE, html, re.IGNORECASE | re.DOTALL)
        if not m or not m.group(1):
            raise ValueError(f"Regex capture found no group: {REGEX_CAPTURE}")
        return m.group(1).strip()

    raise ValueError("You must set either MONITOR_CSS_SELECTOR or MONITOR_REGEX_CAPTURE")

def value_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def generate_pdf(old_value: str, new_value: str, url: str) -> Path:
    ts = now_utc().strftime("%Y-%m-%d_%H%M%SZ")
    filename = PDF_DIR / f"change_{ts}.pdf"

    c = canvas.Canvas(str(filename), pagesize=LETTER)
    width, height = LETTER

    left = 1.0 * inch
    top  = height - 1.0 * inch
    line_gap = 14

    def write_line(text, y):
        c.drawString(left, y, text)
        return y - line_gap

    y = top
    c.setTitle("Monitor Change Report")

    c.setFont("Helvetica-Bold", 14)
    y = write_line("Monitor Change Detected", y)
    c.setFont("Helvetica", 10)
    y = write_line(f"Detected at (UTC): {now_utc().isoformat()}", y)
    y = write_line(f"URL: {url}", y)
    y = write_line("", y)

    c.setFont("Helvetica-Bold", 12)
    y = write_line("Old Value:", y)
    c.setFont("Helvetica", 11)
    y = write_line(old_value if old_value is not None else "(None - first run)", y)
    y = write_line("", y)

    c.setFont("Helvetica-Bold", 12)
    y = write_line("New Value:", y)
    c.setFont("Helvetica", 11)
    y = write_line(new_value, y)

    c.showPage()
    c.save()
    return filename

def send_email(subject: str, body: str, attachments: list[Path] | None = None):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO]):
        print("Email not sent: SMTP env vars not fully configured.", file=sys.stderr)
        return

    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Subject"] = f"{MAIL_SUBJECT_PREFIX} {subject}"
    msg.set_content(body)

    for att in attachments or []:
        with open(att, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=att.name)

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def log(msg: str):
    ensure_dirs()
    ts = now_utc().isoformat()
    line = f"{ts} | {msg}\n"
    with open(LOG_DIR / "monitor.log", "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")

# --------------------
# Core actions
# --------------------
def run_check():
    if not URL:
        raise SystemExit("MONITOR_URL is required.")

    ensure_dirs()
    state = load_state()

    try:
        html = fetch_content(URL)
        current_value = extract_value(html)
        current_hash = value_hash(current_value)
    except Exception as e:
        log(f"ERROR during fetch/extract: {e}")
        return

    if state["last_value_hash"] == current_hash:
        log(f"No change. Value: {current_value}")
        return

    # Change detected
    old_value = state["last_value"]
    pdf_path = generate_pdf(old_value, current_value, URL)

    state["last_value"] = current_value
    state["last_value_hash"] = current_hash
    state["last_change_ts"] = now_utc().isoformat()
    state["changes_today"] = True
    save_state(state)

    subject = "Change detected"
    body = (
        "A change was detected.\n\n"
        f"URL: {URL}\n"
        f"Old value: {old_value}\n"
        f"New value: {current_value}\n"
        f"Time (UTC): {now_utc().isoformat()}\n"
    )
    send_email(subject, body, attachments=[pdf_path])
    log(f"CHANGE detected. Old: {old_value} -> New: {current_value}. PDF: {pdf_path.name}")

def run_daily_summary(force=False):
    """
    Send the once-per-day 'no changes detected' email if:
      - No change occurred today, and
      - We haven't already sent today's summary.
    """
    state = load_state()
    today = today_key()

    already_sent_today = (state.get("last_summary_day") == today)
    if already_sent_today and not force:
        log("Daily summary already sent today; skipping.")
        return

    if state.get("changes_today"):
        log("Changes occurred today; no 'no changes detected' email needed.")
        # Reset the daily changes flag at end of day, and mark summary day to avoid double-send
        state["changes_today"] = False
        state["last_summary_day"] = today
        save_state(state)
        return

    # No changes today -> send the summary email once
    subject = "No changes detected"
    body = (
        f"No changes detected for {today} (UTC).\n"
        f"URL: {URL}\n"
    )
    send_email(subject, body)
    log("Daily summary sent: No changes detected.")
    state["last_summary_day"] = today
    state["changes_today"] = False
    save_state(state)

# --------------------
# CLI
# --------------------
def main():
    parser = argparse.ArgumentParser(description="Check a URL for a single value change; create PDF & send emails.")
    parser.add_argument("--check", action="store_true", help="Perform a single check now.")
    parser.add_argument("--daily-summary", action="store_true", help="Send the once-per-day 'no changes' email if needed.")
    parser.add_argument("--force-summary", action="store_true", help="Force sending summary even if already sent today.")
    args = parser.parse_args()

    if not (args.check or args.daily_summary):
        parser.print_help()
        sys.exit(1)

    if args.check:
        run_check()

    if args.daily_summary:
        run_daily_summary(force=args.force_summary)

if __name__ == "__main__":
    main()
