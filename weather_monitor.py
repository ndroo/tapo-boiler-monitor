#!/usr/bin/env python3
"""
weather_monitor.py - outdoor temperature for the property, logged alongside the boiler readings.

Data: Open-Meteo (https://open-meteo.com, free, no key). The property address is geocoded ONCE
with Nominatim (OpenStreetMap) and cached in stills/weather_geo.json, so change PROPERTY_ADDRESS
(env var or the default below) and the next run re-geocodes.

Each run:
  * merges the last PAST_DAYS of hourly observations into stills/weather.jsonl (one line per
    hour, keyed by UTC hour, so a missed run never leaves a hole and nothing is duplicated)
  * writes stills/weather_latest.json with the current conditions

Usage:  python3 weather_monitor.py --once          (what loop.sh runs every 15 minutes)
        python3 weather_monitor.py --interval 900
"""
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests

PROPERTY_ADDRESS = os.environ.get("PROPERTY_ADDRESS", "180 Beatrice St, Toronto, Ontario, Canada")
PAST_DAYS = int(os.environ.get("WEATHER_PAST_DAYS", "7"))
OUT = Path(os.environ.get("MONITOR_OUT", "stills"))
GEO_FILE, LOG_FILE, LATEST_FILE = OUT / "weather_geo.json", OUT / "weather.jsonl", OUT / "weather_latest.json"
MAX_LINES = 10000                                   # ~13 months of hourly points
UA = "tapo-boiler-monitor/1.0 (https://github.com/ndroo/tapo-boiler-monitor)"
HOURLY_VARS = "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,precipitation,weather_code"

def log(msg): print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)

def geocode(address):
    """Nominatim lookup, cached per address in GEO_FILE."""
    if GEO_FILE.exists():
        try:
            g = json.loads(GEO_FILE.read_text())
            if g.get("address") == address and g.get("lat") is not None:
                return g
        except Exception:
            pass
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"format": "json", "limit": 1, "q": address}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        raise SystemExit(f"could not geocode {address!r}")
    g = {"address": address, "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]),
         "display_name": hits[0].get("display_name"), "geocoded": datetime.now(timezone.utc).isoformat()}
    OUT.mkdir(parents=True, exist_ok=True)
    GEO_FILE.write_text(json.dumps(g, indent=2) + "\n")
    log(f"geocoded {address!r} -> {g['lat']:.5f},{g['lon']:.5f}")
    return g

def fetch(g):
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=30, headers={"User-Agent": UA}, params={
        "latitude": g["lat"], "longitude": g["lon"], "timezone": "UTC",
        "current": HOURLY_VARS, "hourly": HOURLY_VARS, "past_days": PAST_DAYS, "forecast_days": 1})
    r.raise_for_status()
    return r.json()

def cycle():
    g = geocode(PROPERTY_ADDRESS)
    d = fetch(g)
    now = datetime.now(timezone.utc)
    cur = d.get("current", {})
    latest = {"timestamp": now.isoformat(), "observed": cur.get("time", "") + "Z", "address": g["address"],
              "lat": g["lat"], "lon": g["lon"], "temp_c": cur.get("temperature_2m"),
              "apparent_c": cur.get("apparent_temperature"), "humidity": cur.get("relative_humidity_2m"),
              "wind_kmh": cur.get("wind_speed_10m"), "precip_mm": cur.get("precipitation"),
              "weather_code": cur.get("weather_code"), "source": "open-meteo"}
    # merge hourly history (past only) into the log, keyed by UTC hour
    rows = {}
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text().splitlines():
            try:
                o = json.loads(line); rows[o["t"]] = o
            except Exception:
                pass
    h = d.get("hourly", {})
    keys = ["temperature_2m", "apparent_temperature", "relative_humidity_2m", "wind_speed_10m", "precipitation", "weather_code"]
    names = ["temp_c", "apparent_c", "humidity", "wind_kmh", "precip_mm", "weather_code"]
    added = 0
    for i, t in enumerate(h.get("time", [])):
        ts = t + "Z"
        if datetime.fromisoformat(t).replace(tzinfo=timezone.utc) > now:
            continue                                   # forecast hours are not observations
        row = {"t": ts}
        for k, n in zip(keys, names):
            v = (h.get(k) or [None] * len(h["time"]))[i]
            if v is not None:
                row[n] = v
        if row.get("temp_c") is None:
            continue
        if ts not in rows:
            added += 1
        rows[ts] = row
    ordered = [rows[k] for k in sorted(rows)][-MAX_LINES:]
    OUT.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text("".join(json.dumps(r) + "\n" for r in ordered))
    LATEST_FILE.write_text(json.dumps(latest, indent=2) + "\n")
    log(f"outdoor {latest['temp_c']}°C (feels {latest['apparent_c']}°C, {latest['humidity']}% RH, wind {latest['wind_kmh']} km/h) "
        f"at {g['address']} · hourly log {len(ordered)} rows (+{added})")
    return latest

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=900)
    a = ap.parse_args()
    while True:
        try:
            cycle()
        except Exception as e:
            log(f"weather cycle failed: {e}")
            if a.once:
                sys.exit(1)
        if a.once:
            break
        time.sleep(a.interval)

if __name__ == "__main__":
    main()
