#!/usr/bin/env python3
"""
KLAX Temperature Database Agent — v4
All temperatures stored in Celsius to 1 decimal place.

Sources:
  Forecast: api.weather.gov/points -> gridpoints -> forecast
            NWS returns °F — converted to °C on ingest
  Observed: api.weather.gov/stations/KLAX/observations
            NWS returns °C natively — rounded to 1 decimal

Schedule (GitHub Actions cron):
  16:30 UTC = 9:30 AM PDT  → forecast job
  20:00 UTC = 1:00 PM PDT  → observed job
  23:00 UTC = 4:00 PM PDT  → observed job (second check)
"""

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PDT     = ZoneInfo("America/Los_Angeles")
DB_FILE = Path("data/klax_database.json")
DB_FILE.parent.mkdir(exist_ok=True)

KLAX_LAT = 33.9425
KLAX_LON = -118.4081
OBS_URL  = "https://api.weather.gov/stations/KLAX/observations?limit=200"

def log(msg): print(f"[{datetime.now(PDT).strftime('%Y-%m-%d %H:%M:%S PDT')}] {msg}", flush=True)

def f_to_c(f): return round((f - 32) * 5/9, 1)
def c_round(c): return round(c, 1)

def load_db() -> dict:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text())
        except Exception as e:
            log(f"Warning: could not load DB: {e}")
    return {}

def save_db(db: dict):
    DB_FILE.write_text(json.dumps(db, indent=2, sort_keys=True))
    log(f"Database saved — {len(db)} total entries")

def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "KLAX-GHActions/4.0 (research)",
        "Accept":     "application/geo+json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

# ── Gridpoint discovery ───────────────────────────────────────────────────
def get_forecast_url() -> str:
    try:
        raw  = fetch(f"https://api.weather.gov/points/{KLAX_LAT},{KLAX_LON}")
        data = json.loads(raw)
        url  = data["properties"]["forecast"]
        gid  = data["properties"]["gridId"]
        gx   = data["properties"]["gridX"]
        gy   = data["properties"]["gridY"]
        log(f"Gridpoint: {gid}/{gx},{gy}")
        return url
    except Exception as e:
        log(f"Gridpoint lookup failed: {e} — using confirmed fallback LOX/148,41")
        return "https://api.weather.gov/gridpoints/LOX/148,41/forecast"

# ── NWS forecast (stored in °C) ───────────────────────────────────────────
def fetch_forecast(target_date: str) -> dict | None:
    url = get_forecast_url()
    try:
        raw     = fetch(url)
        data    = json.loads(raw)
        periods = data.get("properties", {}).get("periods", [])

        # Pass 1: exact date match
        for p in periods:
            if not p.get("isDaytime"):
                continue
            if p["startTime"][:10] == target_date:
                units = p.get("temperatureUnit", "F")
                fcst_f = p["temperature"]
                fcst_c = f_to_c(fcst_f) if units == "F" else c_round(fcst_f)
                log(f"Forecast {target_date}: {fcst_f}°F = {fcst_c}°C ('{p['name']}')")
                return {
                    "fcst":        fcst_c,
                    "fcst_unit":   "C",
                    "fcst_src":    "nws-api",
                    "fcst_time":   datetime.now(PDT).strftime("%H:%M PDT"),
                    "fcst_period": p["name"],
                }

        # Pass 2: today's daytime expired — take next available
        for p in periods:
            if not p.get("isDaytime"):
                continue
            actual_date = p["startTime"][:10]
            units       = p.get("temperatureUnit", "F")
            fcst_f      = p["temperature"]
            fcst_c      = f_to_c(fcst_f) if units == "F" else c_round(fcst_f)
            log(f"Today expired — logging {actual_date}: {fcst_f}°F = {fcst_c}°C ('{p['name']}')")
            return {
                "fcst":        fcst_c,
                "fcst_unit":   "C",
                "fcst_src":    "nws-api",
                "fcst_time":   datetime.now(PDT).strftime("%H:%M PDT"),
                "fcst_period": p["name"],
                "fcst_date":   actual_date,
            }

        log("No daytime periods found in NWS response")
    except Exception as e:
        log(f"Forecast fetch failed: {e}")
    return None

# ── NWS ASOS observed high (native °C) ───────────────────────────────────
def fetch_observed_highs() -> dict:
    """
    Fetches hourly ASOS observations for KLAX.
    NWS API returns temperature natively in °C.
    Returns {date_str: max_temp_C} for all dates with data.
    """
    try:
        raw  = fetch(OBS_URL)
        data = json.loads(raw)
    except Exception as e:
        log(f"Observations fetch failed: {e}")
        return {}

    daily = {}  # date_str -> list of °C readings
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        ts    = props.get("timestamp")
        if not ts:
            continue
        try:
            utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            pdt_dt = utc_dt.astimezone(PDT)
            ds     = pdt_dt.strftime("%Y-%m-%d")
        except Exception:
            continue

        # Temperature natively in °C from NWS ASOS API
        temp_c = props.get("temperature", {}).get("value")
        if temp_c is None or not isinstance(temp_c, (int, float)):
            continue

        # Sanity check: LAX temps in °C should be 10–45°C
        if not (10 <= temp_c <= 45):
            continue

        if ds not in daily:
            daily[ds] = []
        daily[ds].append(temp_c)

    result = {}
    for ds, temps in daily.items():
        if temps:
            result[ds] = c_round(max(temps))
            log(f"Observed {ds}: {result[ds]}°C (max of {len(temps)} readings)")

    log(f"Observations: {len(result)} days computed")
    return result

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    job       = os.environ.get("KLAX_JOB", "forecast")
    now_pdt   = datetime.now(PDT)
    today_str = now_pdt.strftime("%Y-%m-%d")

    log("=" * 55)
    log(f"KLAX agent v4 — job={job} date={today_str}")
    log(f"Units: Celsius to 1 decimal place")
    log("=" * 55)

    db = load_db()

    if job == "forecast":
        log("Job: fetch NWS forecast → store in °C")
        result = fetch_forecast(today_str)
        if result:
            save_date = result.pop("fcst_date", today_str)
            if save_date not in db:
                db[save_date] = {}
            if db[save_date].get("fcst_src") == "nws-api":
                log(f"Forecast already logged for {save_date} "
                    f"({db[save_date]['fcst']}°C) — skipping")
            else:
                db[save_date].update(result)
                save_db(db)
                log(f"✓ Saved: {save_date} fcst={result['fcst']}°C")
        else:
            log("✗ Could not retrieve forecast")
            sys.exit(1)

    elif job == "observed":
        log("Job: fetch NWS ASOS observed highs → store in °C")
        observed = fetch_observed_highs()
        if not observed:
            log("✗ No observed data returned")
            sys.exit(1)
        changed = False
        for ds, tmax_c in observed.items():
            if ds not in db:
                db[ds] = {}
            old = db[ds].get("obs")
            if old != tmax_c:
                db[ds]["obs"]      = tmax_c
                db[ds]["obs_unit"] = "C"
                db[ds]["obs_src"]  = "nws-asos"
                db[ds]["obs_time"] = now_pdt.strftime("%H:%M PDT")
                log(f"  {ds}: {tmax_c}°C (was {old})")
                changed = True
        if changed:
            save_db(db)
            log("✓ Observed highs saved")
        else:
            log("No changes to observed data")

    log("Run complete.")

if __name__ == "__main__":
    main()
