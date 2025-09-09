#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from twilio.rest import Client

URL           = os.getenv("MONITOR_URL", "").strip()
CSS_SELECTOR  = os.getenv("MONITOR_CSS_SELECTOR", "").strip()
REGEX_CAPTURE = os.getenv("MONITOR_REGEX_CAPTURE", "").strip()
TIMEOUT_SEC   = int(os.getenv("MONITOR_TIMEOUT_SEC", "30"))

STATE_FILE    = Path(os.getenv("MONITOR_STATE_FILE", "./state/monitor_state.json"))
LOG_DIR       = Path(os.getenv("MONITOR_LOG_DIR", "./logs"))

# Optional: if set to "1", call on extraction errors so you don't miss breakage
CALL_ON_ERROR = os.getenv("MONITOR_CALL_ON_ERROR", "0") == "1"

TWILIO_SID  = os.getenv("TWILIO_SID", "")
TWILIO_AUTH = os.getenv("TWILIO_AUTH", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
TWILIO_TO   = os.getenv("TWILIO_TO", "")

def now_utc(): return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
def today_key(): return now_utc().date().isoformat()

def ensure_dirs():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "last_value": None,
        "last_value_hash": None,
        "last_change_ts": None,
        "changes_today": False,
        "last_summary_day": None
    }

def save_state(state): STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def log(msg: str):
    ensure_dirs()
    line = f"{now_utc().isoformat()} | {msg}\n"
    (LOG_DIR / "monitor.log").open("a", encoding="utf-8").write(line)
    print(line, end="")

def fetch_content(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "AO-Monitor/1.0"}, timeout=TIMEOUT_SEC)
    r.raise_for_status()
    return r.text

def extract_value(html: str) -> str:
    if CSS_SELECTOR:
        node = BeautifulSoup(html, "html.parser").select_one(CSS_SELECTOR)
        if not node:
            raise ValueError(f"CSS selector not found: {CSS_SELECTOR}")
        t = node.get_text(strip=True)
        if not t:
            raise ValueError(f"CSS selector empty text: {CSS_SELECTOR}")
        return t
    if REGEX_CAPTURE:
        m = re.search(REGEX_CAPTURE, html, re.IGNORECASE | re.DOTALL)
        if not m or not m.group(1):
            raise ValueError(f"Regex capture found no group: {REGEX_CAPTURE}")
        return m.group(1).strip()
    raise ValueError("Set MONITOR_CSS_SELECTOR or MONITOR_REGEX_CAPTURE")

def value_hash(s: str):
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# ---------- Twilio ----------
def _twilio():
    if not all([TWILIO_SID, TWILIO_AUTH, TWILIO_FROM, TWILIO_TO]):
        raise RuntimeError("Twilio env vars missing (TWILIO_*).")
    return Client(TWILIO_SID, TWILIO_AUTH)

def send_call(message: str):
    c = _twilio()
    call = c.calls.create(to=TWILIO_TO, from_=TWILIO_FROM, twiml=f'<Response><Say>{message}</Say></Response>')
    print(f"Placed call SID={call.sid}")

# ---------- Core ----------
def run_check(current_value_override: str | None = None):
    if not URL and current_value_override is None:
        raise SystemExit("MONITOR_URL is required (unless using --inject-value).")

    ensure_dirs()
    state = load_state()

    try:
        if current_value_override is None:
            html = fetch_content(URL)
            current_value = extract_value(html)
        else:
            current_value = current_value_override
        current_hash = value_hash(current_value)
    except Exception as e:
        log(f"ERROR during fetch/extract: {e}")
        if CALL_ON_ERROR:
            try:
                send_call("Website monitor error. Extraction failed. Please check the selector or regex.")
            except Exception as e2:
                log(f"ERROR placing error-call: {e2}")
        return

    if state["last_value_hash"] == current_hash:
        log(f"No change. Value: {current_value}")
        return

    old_value = state["last_value"]
    state["last_value"] = current_value
    state["last_value_hash"] = current_hash
    state["last_change_ts"] = now_utc().isoformat()
    state["changes_today"] = True
    save_state(state)

    try:
        send_call(f"A change was detected. New value is {current_value}.")
        log(f"CHANGE detected. Old: {old_value} -> New: {current_value}. Call sent.")
    except Exception as e:
        log(f"ERROR sending change-call: {e}")

def run_daily_summary(force=False):
    st = load_state()
    today = today_key()
    if st.get("last_summary_day") == today and not force:
        log("Daily summary already sent today; skipping.")
        return

    if st.get("changes_today"):
        st["changes_today"] = False
        st["last_summary_day"] = today
        save_state(st)
        log("Changes occurred today; no daily 'no changes' call.")
        return

    try:
        send_call("No changes detected today for the monitored page.")
        log("Daily summary call sent (no changes).")
    except Exception as e:
        log(f"ERROR sending daily call: {e}")

    st["last_summary_day"] = today
    st["changes_today"] = False
    save_state(st)

# ---------- Test / Probe helpers ----------
def seed_state_last_value(val: str | None):
    st = load_state()
    if val is None:
        st["last_value"] = None
        st["last_value_hash"] = None
    else:
        st["last_value"] = val
        st["last_value_hash"] = value_hash(val)
    save_state(st)
    log(f"Seeded state last_value to: {val}")

def probe_once():
    """Fetch & print the current extracted value (no compare, no calls)."""
    try:
        html = fetch_content(URL)
        val = extract_value(html)
        print(f"[PROBE] Extracted value: {val}")
        log(f"[PROBE] Extracted value: {val}")
    except Exception as e:
        log(f"[PROBE ERROR] {e}")
        if CALL_ON_ERROR:
            try:
                send_call("Website monitor probe failed. Check selector or regex.")
            except Exception as e2:
                log(f"ERROR placing probe error-call: {e2}")

def main():
    p = argparse.ArgumentParser(description="URL monitor with Twilio call. Includes probe & test hooks.")
    p.add_argument("--check", action="store_true")
    p.add_argument("--daily-summary", action="store_true")
    p.add_argument("--force-summary", action="store_true")
    p.add_argument("--inject-value", type=str, help="Bypass fetch; use this as current value.")
    p.add_argument("--set-state", type=str, help="Seed last_value (and hash) for tests.")
    p.add_argument("--reset-state", action="store_true", help="Clear last_value/hash.")
    p.add_argument("--probe", action="store_true", help="Fetch & print the current extracted value only.")
    p.add_argument("--test-call", action="store_true", help="Immediate test call.")
    args = p.parse_args()

    if args.reset_state:  seed_state_last_value(None); return
    if args.set_state is not None: seed_state_last_value(args.set_state); return
    if args.test_call:    send_call("This is a test call from the website monitor."); return
    if args.probe:        probe_once(); return
    if args.check:        run_check(current_value_override=args.inject_value); return
    if args.daily_summary:run_daily_summary(force=args.force_summary); return
    p.print_help(); sys.exit(1)

if __name__ == "__main__":
    main()
