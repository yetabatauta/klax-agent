#!/usr/bin/env python3
"""
KLAX Temperature Database Agent — GitHub Actions version
Runs on GitHub's servers via cron schedule. No local machine needed.

Schedule (defined in workflow YAML):
  - 16:30 UTC = 9:30 AM PDT  → fetch NWS forecast for today
  - 20:00 UTC = 1:00 PM PDT  → fetch CF6 observed (first check)
  - 23:00 UTC = 4:00 PM PDT  → fetch CF6 observed (second check)
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PDT     = ZoneInfo("America/Los_Angeles")
DB_FILE = Path("data/klax_database.json")
DB_FILE.parent.mkdir(exist_ok=True)

KLAX_LAT = 33.9425
KLAX_LON = -118.4081
CF6_URL  = "https://tgftp.nws.noaa.gov/data/raw/cx/cxus56.klox.cf6.lax.txt"

def log(msg): print(f"[{datetime.now(PDT).strftime('%H:%M:%S PDT')}] {msg}", flush=True)

def load_db() -> dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text())
        except Exception as e:
            log(f"Warning: could not load DB: {e}")
    return {}

def save_db(db: dict):
    DB_FILE.write_text(json.dumps(db, indent=2, sort_keys=True))
    log(f"Database saved ({len(db)} entries)")

def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "KLAX-GHActions/2.0 (research)",
        "Accept":     "application/geo+json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def get_forecast_url() -> str:
    try:
        raw  = fetch(f"https://api.weather.gov/points/{KLAX_LAT},{KLAX_LON}")
        data = json.loads(raw)
        url  = data["properties"]["forecast"]
        gid  = data["properties"]["gridId"]
        gx   = data["properties"]["gridX"]
        gy   = data["properties"]["gridY"]
        log(f"Gridpoint verified: {gid}/{gx},{gy}")
        return url
    except Exception as e:
        log(f"Gridpoint discovery failed: {e} — using fallback LOX/148,41")
        return "https://api.weather.gov/gridpoints/LOX/148,41/forecast"

def fetch_forecast(target_date: str) -> dict | None:
    """
    Fetches NWS high temp forecast for target_date.
    If the daytime period for today has expired (after ~6 PM),
    the NWS API rolls forward — we accept the next available
    daytime period and note the actual date it covers.
    """
    url = get_forecast_url()
    try:
        raw  = fetch(url)
        data = json.loads(raw)
        periods = data.get("properties", {}).get("periods", [])

        # First pass: try to find exact date match
        for period in periods:
            if not period.get("isDaytime"):
                continue
            if period["startTime"][:10] == target_date:
                fcst  = period["temperature"]
                units = period.get("temperatureUnit", "F")
                if units != "F":
                    log(f"ERROR: unexpected unit {units}")
                    return None
                log(f"NWS forecast for {target_date}: {fcst}°F ('{period['name']}')")
                return {
                    "fcst":        fcst,
                    "fcst_src":    "nws-api",
                    "fcst_time":   datetime.now(PDT).strftime("%H:%M PDT"),
                    "fcst_period": period["name"],
                }

        # Second pass: today's daytime period has expired (past ~6 PM)
        # Take the FIRST available daytime period and log it for its actual date
        for period in periods:
            if not period.get("isDaytime"):
                continue
            actual_date = period["startTime"][:10]
            fcst        = period["temperature"]
            units       = period.get("temperatureUnit", "F")
            if units != "F":
                continue
            log(f"Today's period expired — using next daytime: "
                f"{actual_date} = {fcst}°F ('{period['name']}')")
            return {
                "fcst":        fcst,
                "fcst_src":    "nws-api",
                "fcst_time":   datetime.now(PDT).strftime("%H:%M PDT"),
                "fcst_period": period["name"],
                "fcst_date":   actual_date,  # actual date this forecast covers
            }

        log("No daytime periods found at all in NWS response")
    except Exception as e:
        log(f"Forecast fetch failed: {e}")
    return None

def parse_cf6(text: str) -> dict:
    yr_m = re.search(r"YEAR:\s+(\d{4})", text)
    mo_m = re.search(r"MONTH:\s+(\w+)", text)
    if not yr_m or not mo_m:
        return {}
    year = int(yr_m.group(1))
    months = {
        "JANUARY":1,"FEBRUARY":2,"MARCH":3,"APRIL":4,"MAY":5,"JUNE":6,
        "JULY":7,"AUGUST":8,"SEPTEMBER":9,"OCTOBER":10,"NOVEMBER":11,"DECEMBER":12,
    }
    month = months.get(mo_m.group(1).upper())
    if not month:
        return {}
    result = {}
    for line in text.splitlines():
        m = re.match(r"^\s{0,2}(\d{1,2})\s+(\d{2,3})\s+(\d{2,3})", line)
        if not m:
            continue
        day, tmax = int(m.group(1)), int(m.group(2))
        if not (1 <= day <= 31 and 40 <= tmax <= 120):
            continue
        result[f"{year}-{month:02d}-{day:02d}"] = tmax
    return result

def fetch_cf6() -> dict:
    try:
        text   = fetch(CF6_URL)
        parsed = parse_cf6(text)
        log(f"CF6LAX: {len(parsed)} days parsed")
        return parsed
    except Exception as e:
        log(f"CF6 fetch failed: {e}")
    return {}

def main():
    job       = os.environ.get("KLAX_JOB", "forecast")
    now_pdt   = datetime.now(PDT)
    today_str = now_pdt.strftime("%Y-%m-%d")

    log(f"KLAX agent starting — job={job}, date={today_str}")
    db = load_db()

    if job == "forecast":
        log("Running: NWS forecast fetch")
        result = fetch_forecast(today_str)
        if result:
            # Use fcst_date if present (when today's period expired), else today
            save_date = result.pop("fcst_date", today_str)
            if save_date not in db:
                db[save_date] = {}
            # Don't overwrite an existing real entry for that date
            if db[save_date].get("fcst_src") == "nws-api":
                log(f"Forecast already logged for {save_date}: "
                    f"{db[save_date]['fcst']}°F — skipping")
            else:
                db[save_date].update(result)
                save_db(db)
                log(f"✓ Forecast saved for {save_date}: {result['fcst']}°F")
        else:
            log("✗ Could not retrieve forecast")
            sys.exit(1)

    elif job == "observed":
        log("Running: CF6LAX observed high fetch")
        parsed = fetch_cf6()
        if not parsed:
            log("✗ No data from CF6")
            sys.exit(1)
        changed = False
        for ds, tmax in parsed.items():
            if ds not in db:
                db[ds] = {}
            if db[ds].get("obs") != tmax or db[ds].get("obs_src") != "cf6":
                old = db[ds].get("obs", "—")
                db[ds]["obs"]      = tmax
                db[ds]["obs_src"]  = "cf6"
                db[ds]["obs_time"] = now_pdt.strftime("%H:%M PDT")
                log(f"  {ds}: {tmax}°F (was {old})")
                changed = True
        if changed:
            save_db(db)
            log("✓ Observed highs updated")
        else:
            log("No changes to observed data")

    log("Agent run complete.")

if __name__ == "__main__":
    main()
