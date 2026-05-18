# KLAX Temperature Database Agent

Automatically logs NWS 8 AM forecast and CF6-verified observed highs for Los Angeles International Airport (KLAX) every day. Runs entirely on GitHub's free servers — no local machine, no sleep issues, no cost.

## What it does

| Time (PDT) | Action |
|---|---|
| 9:30 AM | Fetches today's NWS forecast high from `api.weather.gov` |
| 1:00 PM | Fetches CF6LAX observed highs from `tgftp.nws.noaa.gov` |
| 4:00 PM | Second CF6 check (catches late-publishing updates) |

All values are written to `data/klax_database.json` and committed automatically.

## Data sources

- **Observed highs**: `tgftp.nws.noaa.gov/data/raw/cx/cxus56.klox.cf6.lax.txt`  
  NWS Preliminary Local Climatological Data (WS Form F-6). Official, quality-controlled.

- **NWS forecast**: `api.weather.gov/gridpoints/LOX/{x},{y}/forecast`  
  Gridpoint auto-discovered from KLAX coordinates (33.9425°N, 118.4081°W).  
  Captures first human-reviewed morning forecast after 9 AM AFD issuance.

## Setup (one time, ~3 minutes)

### 1. Fork or create this repo on GitHub
Go to github.com, create a new repository, name it `klax-agent` (or anything you like).

### 2. Upload these files
Upload all files in this folder to the repository root. Make sure `.github/workflows/klax_agent.yml` is in the right place.

### 3. Enable Actions
Go to your repo → **Actions** tab → click **"I understand my workflows, go ahead and enable them"**.

### 4. Test it manually
Go to **Actions** → **KLAX Temperature Agent** → **Run workflow** → choose `forecast` → **Run workflow**.  
Watch the logs. If it passes, you're done.

That's it. The agent runs automatically from that point — every day, forever, for free.

## Viewing the database

The database is always at `data/klax_database.json` in your repo.  
Each entry looks like:

```json
"2026-05-18": {
  "fcst": 70,
  "fcst_src": "nws-api",
  "fcst_time": "09:31 PDT",
  "fcst_period": "Today",
  "obs": 69,
  "obs_src": "cf6",
  "obs_time": "13:02 PDT"
}
```

## Stopping the agent
Go to **Actions** → **KLAX Temperature Agent** → the three-dot menu → **Disable workflow**.
