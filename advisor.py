#!/usr/bin/env python3
"""Recommend windows, fans, AC, heat, and what to wear from the outdoor forecast; push changes via ntfy."""

import json
import os
import sys
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

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

# Clothing thresholds on the feels-like temperature (°F), plus the swing that triggers layering the rain chance that calls for rain gear, and the UV index that calls for sunscreen.
CLOTHING_DEFAULTS = {
    "too_cold": 0.0,
    "hat_gloves_below": 35.0,
    "coat_below": 45.0,
    "sweater_below": 60.0,
    "shorts_min": 75.0,
    "layer_swing": 20.0,
    "rain_chance": 50,
    "uv_min": 3.0,
}

OUTFITS = {
    "frigid": "way too cold, stay in if you can",
    "freezing": "pants, coat, hat, gloves",
    "cold": "pants, coat",
    "cool": "pants, sweater",
    "mild": "pants, t-shirt",
    "warm": "shorts, t-shirt",
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
    clothing = {**CLOTHING_DEFAULTS, **raw.get("clothing", {})}
    return float(location["lat"]), float(location["lon"]), comfort, clothing


def fetch_forecast(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,dew_point_2m,apparent_temperature",
        "hourly": "temperature_2m,dew_point_2m,apparent_temperature,precipitation_probability,uv_index",
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


class Outfit(NamedTuple):
    tier: str
    rain: tuple | None  # (time, chance %) at the peak chance of rain in the lookahead
    sun: tuple | None  # (time, UV index) at the peak UV in the lookahead
    layers: tuple | None  # two (when, feels-like °F) points, lowest and highest, in time order

    @property
    def key(self):
        """Identifies the outfit for change detection; the layering, rain, and UV numbers don't count."""
        return self.tier + ("+rain" if self.rain else "") + ("+sun" if self.sun else "")


def outfit_tier(feels, o):
    if feels < o["too_cold"]:
        return "frigid"
    if feels < o["hat_gloves_below"]:
        return "freezing"
    if feels < o["coat_below"]:
        return "cold"
    if feels < o["sweater_below"]:
        return "cool"
    if feels < o["shorts_min"]:
        return "mild"
    return "warm"


def hourly_window(forecast, field, lookahead, after_now=False):
    """Return (time, value) for the current hour (unless after_now) and the hours after it, within the lookahead."""
    hourly = forecast["hourly"]
    now = forecast["current"]["time"]
    pairs = list(zip(hourly["time"], hourly[field]))
    if after_now:
        pairs = [(t, v) for t, v in pairs if t > now]
    else:
        pairs = pairs[max(sum(t <= now for t, _ in pairs) - 1, 0) :]
    return [(t, v) for t, v in pairs[: int(lookahead)] if v is not None]


def peak(window, minimum):
    """Return the (time, rounded value) of the highest value in the window, if it reaches minimum."""
    if not window:
        return None
    t, v = max(window, key=lambda pair: pair[1])
    return (t, round(v)) if v >= minimum else None


def outfit_advice(forecast, o, lookahead):
    feels = round(forecast["current"]["apparent_temperature"])
    ahead = [(format_hour(t), round(v)) for t, v in hourly_window(forecast, "apparent_temperature", lookahead, True)]
    points = [("now", feels), *ahead]
    # Order the coldest and warmest points by when they happen so the note reads in time order.
    lo, hi = min(points, key=lambda p: p[1]), max(points, key=lambda p: p[1])
    layers = None
    if hi[1] - lo[1] >= o["layer_swing"]:
        layers = tuple(sorted((lo, hi), key=points.index))
    rain = peak(hourly_window(forecast, "precipitation_probability", lookahead), o["rain_chance"])
    sun = peak(hourly_window(forecast, "uv_index", lookahead), o["uv_min"])
    return Outfit(outfit_tier(feels, o), rain, sun, layers)


def wear_lines(outfit, unit):
    """Return the "Wear:" line, plus a layering line when feels-like swings a lot."""
    items = [OUTFITS[outfit.tier]]
    if outfit.rain:
        t, chance = outfit.rain
        items.append(f"rain jacket ({chance}% at {format_hour(t)})")
    if outfit.sun:
        t, uv = outfit.sun
        items.append(f"sunscreen (UV {uv} at {format_hour(t)})")
    lines = ["Wear: " + " · ".join(items)]
    if outfit.layers:
        points = [f"{degrees(feels, unit)} {when if when == 'now' else 'at ' + when}" for when, feels in outfit.layers]
        lines.append(f"Layer up: feels like {points[0]}, {points[1]}")
    return lines


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


def read_last_state():
    """Return (mode, outfit key) from the state file; the outfit key is None in the older mode-only format."""
    try:
        mode, _, outfit = STATE_PATH.read_text().strip().partition("|")
    except FileNotFoundError:
        return None, None
    return mode, outfit or None


def worth_pushing(last_state, mode, outfit):
    """Mode and tier changes push; rain and sunscreen only when they turn on.

    Dropping them ("sunscreen off at 6 PM") isn't news, but it still updates the saved
    state so they push again next time they turn on.
    """
    last_mode, last_key = last_state
    if last_key is None:  # no state yet, or the older mode-only format
        return True
    last_tier, *last_gear = last_key.split("+")
    gear = outfit.key.split("+")[1:]
    return mode != last_mode or outfit.tier != last_tier or bool(set(gear) - set(last_gear))


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
    lat, lon, comfort, clothing = load_config()
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        fail("NTFY_TOPIC is not set; export it to your ntfy topic name.")

    forecast = fetch_forecast(lat, lon)
    current = forecast["current"]
    # Classify on whole degrees so the decision matches the rounded values shown.
    temp, dew = round(current["temperature_2m"]), round(current["dew_point_2m"])
    mode = classify(temp, dew, comfort)
    changes = upcoming_changes(forecast, mode, comfort)
    outfit = outfit_advice(forecast, clothing, comfort["lookahead_hours"])
    print(build_summary(temp, dew, mode, changes, comfort["lookahead_hours"]))
    print("\n" + "\n".join(wear_lines(outfit, "F")))

    # Short notification in each unit, keyed by the topic it goes to.
    notifications = {}
    for dest, unit in ((topic, "F"), (f"{topic}-Metric", "C")):
        title, body = notification(temp, dew, mode, changes, comfort, unit)
        notifications[dest] = (title, "\n".join([body, *wear_lines(outfit, unit)]))
    for title, body in notifications.values():
        print(f"\n{title}\n{body}")

    last_state = read_last_state()
    pushed = worth_pushing(last_state, mode, outfit)
    if pushed:
        for dest, (title, body) in notifications.items():
            push(dest, title, body)
    if (mode, outfit.key) != last_state:
        STATE_PATH.write_text(f"{mode}|{outfit.key}\n")
    print(f"{'Pushed to ntfy' if pushed else 'No push'} (mode {mode}, outfit {outfit.key}).")


if __name__ == "__main__":
    main()
