#!/usr/bin/env python3
"""Recommend windows, fans, AC, or heat from the outdoor forecast and push changes via ntfy."""

import json
import os
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
STATE_PATH = Path.home() / ".weather_advisor_state"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NTFY_URL = "https://ntfy.sh/"

DEFAULTS = {
    "cold": 55.0,
    "open_min": 60.0,
    "open_max": 75.0,
    "hot": 82.0,
    "dry_dew": 60.0,
    "muggy_dew": 65.0,
    "lookahead_hours": 12,
}

ACTIONS = {
    "heat": "Close windows, run the heat",
    "ac": "Close windows, run the AC",
    "windows": "Open the windows",
    "fan": "Use fans",
    "closed": "Keep windows closed, no HVAC needed",
}


def fail(message):
    sys.exit(f"weather-advisor: {message}")


def load_config():
    setup_hint = "copy config.example.toml to config.toml and fill in your lat/lon."
    if not CONFIG_PATH.exists():
        fail(f"{CONFIG_PATH.name} not found; {setup_hint}")
    with CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)

    location = raw.get("location", {})
    missing = [key for key in ("lat", "lon") if key not in location]
    if missing:
        fail(f"missing {' and '.join(missing)} in [location]; {setup_hint}")

    comfort = {**DEFAULTS, **raw.get("comfort", {})}
    return float(location["lat"]), float(location["lon"]), comfort


def fetch_forecast(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,dew_point_2m",
        "hourly": "temperature_2m,dew_point_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 2,
    }
    url = f"{FORECAST_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def classify(temp, dew, c):
    if temp < c["cold"]:
        return "heat"
    if temp > c["hot"] or dew > c["muggy_dew"]:
        return "ac"
    if c["open_min"] <= temp <= c["open_max"] and dew < c["dry_dew"]:
        return "windows"
    if temp > c["open_max"]:
        return "fan"
    return "closed"


def upcoming_changes(forecast, mode, c):
    """Return (time, mode, temp, dew) for each hour in the lookahead where the mode changes."""
    hourly = forecast["hourly"]
    now = forecast["current"]["time"]
    hours = [
        (t, temp, dew)
        for t, temp, dew in zip(hourly["time"], hourly["temperature_2m"], hourly["dew_point_2m"])
        if t > now and temp is not None and dew is not None
    ][: int(c["lookahead_hours"])]

    changes = []
    for t, temp, dew in hours:
        next_mode = classify(temp, dew, c)
        if next_mode != mode:
            changes.append((t, next_mode, temp, dew))
            mode = next_mode
    return changes


def format_time(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt:%a} {dt.hour % 12 or 12} {dt:%p}"


def build_summary(temp, dew, mode, changes, lookahead):
    lines = [f"Now: {temp:.0f}°F, dew point {dew:.0f}°F -> {ACTIONS[mode]}"]
    if changes:
        lines.append(f"Changes in the next {lookahead}h:")
        for t, m, t_temp, t_dew in changes:
            lines.append(f"  {format_time(t)}: {ACTIONS[m]} ({t_temp:.0f}°F, dew {t_dew:.0f}°F)")
    else:
        lines.append(f"No changes in the next {lookahead}h.")
    return "\n".join(lines)


def read_last_mode():
    try:
        return STATE_PATH.read_text().strip()
    except FileNotFoundError:
        return None


def push(topic, title, body):
    req = urllib.request.Request(
        NTFY_URL + urllib.parse.quote(topic),
        data=body.encode("utf-8"),
        headers={"Title": title},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def main():
    lat, lon, comfort = load_config()
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        fail("NTFY_TOPIC is not set; export it to your ntfy topic name.")

    forecast = fetch_forecast(lat, lon)
    current = forecast["current"]
    temp, dew = current["temperature_2m"], current["dew_point_2m"]
    mode = classify(temp, dew, comfort)
    changes = upcoming_changes(forecast, mode, comfort)
    summary = build_summary(temp, dew, mode, changes, comfort["lookahead_hours"])
    print(summary)

    if mode != read_last_mode():
        push(topic, ACTIONS[mode], summary)
        STATE_PATH.write_text(mode + "\n")
        print(f"Pushed to ntfy (mode changed to {mode}).")
    else:
        print(f"Mode unchanged ({mode}); no push.")


if __name__ == "__main__":
    main()
