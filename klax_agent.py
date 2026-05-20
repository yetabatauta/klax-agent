#!/usr/bin/env python3
"""
KLAX Temperature Database Agent — v3
Fixed: observed high now from NWS API observations (JSON, no parsing ambiguity)
Fixed: forecast skips if already logged for today
Fixed: proper date handling across midnight

Sources:
  Forecast: api.weather.gov/points -> gridpoints -> forecast (JSON, °F confirmed)
  Observed: api.weather.gov/stations/KLAX/observations (JSON, hourly ASOS)
            Computes daily max from all hourly readings in PDT calendar day
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PDT     = ZoneInfo("America/Los_Angeles")
DB_FILE = Path("data/klax_database.json")
DB_FILE.parent.mkdir(exist_ok=True)

KLAX_LAT  = 33.9425
KLAX_LON  = -118.4081
OBS_URL   = "https://api.weather.gov/stations/KLAX/observations?limit=200"

def log(msg): print(f"[{datetime.now(PDT).strftime('%Y-%m-%d %H:%M:%S PDT')}] {msg}", flush=True)

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
        "User-Agent": "KLAX-GHActions/3.0 (research)",
        "Accept":     "application/geo+json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

# ── NWS forecast ──────────────────────────────────────────────────────────
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

def fetch_forecast(target_date: str) -> dict | None:
    url = get_forecast_url()
    try:
        raw     = fetch(url)
        data    = json.loads(raw)
        periods = data.get("properties", {}).get("periods", [])

        # Look for daytime period matching target date
        for period in periods:
            if not period.get("isDaytime"):
                continue
            period_date = period["startTime"][:10]
            if period_date == target_date:
                fcst  = period["temperature"]
                units = period.get("temperatureUnit", "F")
                if units != "F":
                    log(f"ERROR: unexpected temperature unit '{units}'")
                    return None
                log(f"Forecast for {target_date}: {fcst}°F ('{period['name']}')")
                return {
                    "fcst":        fcst,
                    "fcst_src":    "nws-api",
                    "fcst_time":   datetime.now(PDT).strftime("%H:%M PDT"),
                    "fcst_period": period["name"],
                }

        # Today's daytime period has expired — log next available daytime date
        for period in periods:
            if not period.get("isDaytime"):
                continue
            actual_date = period["startTime"][:10]
            fcst        = period["temperature"]
            units       = period.get("temperatureUnit", "F")
            if units != "F":
                continue
            log(f"Today daytime expired — logging {actual_date}: {fcst}°F ('{period['name']}')")
            return {
                "fcst":        fcst,
                "fcst_src":    "nws-api",
                "fcst_time":   datetime.now(PDT).strftime("%H:%M PDT"),
                "fcst_period": period["name"],
                "fcst_date":   actual_date,
            }

        log("No daytime periods found in NWS forecast response")
    except Exception as e:
        log(f"Forecast fetch failed: {e}")
    return None

# ── NWS ASOS observed high ────────────────────────────────────────────────
def fetch_observed_highs() -> dict:
    """
    Fetches hourly ASOS observations for KLAX from NWS API.
    Returns {date_str: max_temp_F} for all dates with data.
    Uses structured JSON — no text parsing, no ambiguity.
    Temperature confirmed in °C, converted to °F.
    """
    try:
        raw  = fetch(OBS_URL)
        data = json.loads(raw)
    except Exception as e:
        log(f"Observations fetch failed: {e}")
        return {}

    # Build daily max from all hourly readings
    daily = {}  # date_str -> list of temps in F
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        ts    = props.get("timestamp")
        if not ts:
            continue

        # Convert UTC timestamp to PDT date
        try:
            utc_dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            pdt_dt  = utc_dt.astimezone(PDT)
            ds      = pdt_dt.strftime("%Y-%m-%d")
        except Exception:
            continue

        # Temperature in °C — confirmed field name is "temperature"
        temp_c = props.get("temperature", {}).get("value")
        if temp_c is None or not isinstance(temp_c, (int, float)):
            continue

        # Convert to °F, round to 1 decimal
        temp_f = round(temp_c * 9/5 + 32, 1)

        # Sanity check: LAX highs should be 50–115°F
        if not (50 <= temp_f <= 115):
            continue

        if ds not in daily:
            daily[ds] = []
        daily[ds].append(temp_f)

    # Take max for each day
    result = {}
    for ds, temps in daily.items():
        if temps:
            result[ds] = round(max(temps), 1)
            log(f"Observed {ds}: max {result[ds]}°F from {len(temps)} readings")

    log(f"Observations: computed daily max for {len(result)} days")
    return result

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    job       = os.environ.get("KLAX_JOB", "forecast")
    now_pdt   = datetime.now(PDT)
    today_str = now_pdt.strftime("%Y-%m-%d")

    log(f"=" * 55)
    log(f"KLAX agent v3 — job={job} date={today_str}")
    log(f"=" * 55)

    db = load_db()

    if job == "forecast":
        log("Job: fetch NWS forecast")
        result = fetch_forecast(today_str)
        if result:
            save_date = result.pop("fcst_date", today_str)
            if save_date not in db:
                db[save_date] = {}
            if db[save_date].get("fcst_src") == "nws-api":
                log(f"Forecast already logged for {save_date} "
                    f"({db[save_date]['fcst']}°F) — skipping")
            else:
                db[save_date].update(result)
                save_db(db)
                log(f"✓ Saved forecast {save_date}: {result['fcst']}°F")
        else:
            log("✗ Could not retrieve forecast")
            sys.exit(1)

    elif job == "observed":
        log("Job: fetch observed highs from NWS ASOS")
        observed = fetch_observed_highs()
        if not observed:
            log("✗ No observed data returned")
            sys.exit(1)

        changed = False
        for ds, tmax in observed.items():
            if ds not in db:
                db[ds] = {}
            old = db[ds].get("obs")
            # Only update if new value differs or no value yet
            if old != tmax:
                db[ds]["obs"]      = tmax
                db[ds]["obs_src"]  = "nws-asos"
                db[ds]["obs_time"] = now_pdt.strftime("%H:%M PDT")
                log(f"  Updated {ds}: {tmax}°F (was {old})")
                changed = True

        if changed:
            save_db(db)
            log("✓ Observed highs saved")
        else:
            log("No changes to observed data")

    log("Run complete.")

if __name__ == "__main__":
    main()
