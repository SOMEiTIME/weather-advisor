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
NTFY_URL = "https://ntfy.sh"

DEFAULTS = {
    "cold": 55.0,
    "crack_min": 55.0,
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
    "crack": "Open the windows a little",
    "fan": "Use fans",
    "closed": "Keep windows closed, no HVAC needed",
}

# Short (emoji, phrase) for notifications.
LABELS = {
    "heat": ("🔥", "heat on"),
    "ac": ("❄️", "AC on"),
    "windows": ("🪟", "open windows"),
    "crack": ("🪟", "open windows a little"),
    "fan": ("🌀", "fans on"),
    "closed": ("🏠", "close up"),
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
    if c["crack_min"] <= temp < c["open_min"] and dew < c["dry_dew"]:
        return "crack"
    if temp > c["open_max"]:
        return "fan"
    return "closed"


def upcoming_changes(forecast, mode, c):
    """Return (time, mode, temp, dew) for each hour in the lookahead where the mode changes."""
    hourly = forecast["hourly"]
    now = forecast["current"]["time"]
    hours = [
        (t, round(temp), round(dew))
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


def format_hour(iso):
    dt = datetime.fromisoformat(iso)
    return f"{dt.hour % 12 or 12} {dt:%p}"


def format_time(iso):
    return f"{datetime.fromisoformat(iso):%a} {format_hour(iso)}"


def degrees(deg_f, unit):
    """Format a °F value as whole degrees in unit ("F" or "C")."""
    value = (deg_f - 32) * 5 / 9 if unit == "C" else deg_f
    return f"{value:.0f}°{unit}"


def dew_reason(mode, temp, dew, c):
    """Return "muggy" or "humid" when the dew point, not the temperature, decided the mode."""
    if mode == "ac" and dew > c["muggy_dew"] and temp <= c["hot"]:
        return "muggy"
    if mode == "closed" and c["crack_min"] <= temp <= c["open_max"]:
        return "humid"
    return None


def notification(temp, dew, mode, changes, c, unit):
    """Return (title, body) for a short phone notification, e.g. ("🪟 Open windows", "69°F now · close up at 7 PM")."""
    emoji, phrase = LABELS[mode]
    title = f"{emoji} {phrase[0].upper()}{phrase[1:]}"

    now = f"{degrees(temp, unit)} now"
    reason = dew_reason(mode, temp, dew, c)
    if reason:
        now += f", {reason} (dew {degrees(dew, unit)})"

    parts = [now]
    for t, m, t_temp, t_dew in changes[:2]:
        part = f"{LABELS[m][1]} at {format_hour(t)}"
        if dew_reason(m, t_temp, t_dew, c):
            part += f" (dew {degrees(t_dew, unit)})"
        parts.append(part)
    if not changes:
        parts.append(f"no change for {c['lookahead_hours']}h")
    return title, " · ".join(parts)


def build_summary(temp, dew, mode, changes, lookahead):
    lines = [f"Now: {degrees(temp, "F")}, dew point {degrees(dew, "F")} -> {ACTIONS[mode]}"]
    if changes:
        lines.append(f"Changes in the next {lookahead}h:")
        for t, m, t_temp, t_dew in changes:
            lines.append(
                f"  {format_time(t)}: {ACTIONS[m]} ({degrees(t_temp, "F")}, dew {degrees(t_dew, "F")})"
            )
    else:
        lines.append(f"No changes in the next {lookahead}h.")
    return "\n".join(lines)


def read_last_mode():
    try:
        return STATE_PATH.read_text().strip()
    except FileNotFoundError:
        return None


def push(topic, title, body):
    # JSON publishing keeps the emoji title in UTF-8; HTTP headers only allow Latin-1.
    req = urllib.request.Request(
        NTFY_URL,
        data=json.dumps({"topic": topic, "title": title, "message": body}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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
    # Classify on whole degrees so the decision matches the rounded values shown.
    temp, dew = round(current["temperature_2m"]), round(current["dew_point_2m"])
    mode = classify(temp, dew, comfort)
    changes = upcoming_changes(forecast, mode, comfort)
    print(build_summary(temp, dew, mode, changes, comfort["lookahead_hours"]))

    # Short notification in each unit, keyed by the topic it goes to.
    notifications = {
        topic: notification(temp, dew, mode, changes, comfort, "F"),
        f"{topic}-Metric": notification(temp, dew, mode, changes, comfort, "C"),
    }
    for title, body in notifications.values():
        print(f"\n{title}\n{body}")

    if mode != read_last_mode():
        for dest, (title, body) in notifications.items():
            push(dest, title, body)
        STATE_PATH.write_text(mode + "\n")
        print(f"Pushed to ntfy (mode changed to {mode}).")
    else:
        print(f"Mode unchanged ({mode}); no push.")


if __name__ == "__main__":
    main()
