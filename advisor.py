#!/usr/bin/env python3
"""Recommend windows, fans, AC, heat, and what to wear from the outdoor forecast; push changes via ntfy."""

import json
import os
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
STATE_PATH = Path.home() / ".weather_advisor_state.json"
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
}

NOTIFY_DEFAULTS = {
    "lookahead_hours": 12,
    "push_cooldown_hours": 4,
    "quiet_start": None,  # hour (0-23, local) pushes pause from; None leaves quiet hours off
    "quiet_end": None,  # hour (0-23, local) pushes resume at
}

# Feels-like (°F) outfit thresholds, plus the triggers for layering, rain gear and sunscreen.
CLOTHING_DEFAULTS = {
    "too_cold": 0.0,
    "hat_gloves_below": 35.0,
    "coat_below": 45.0,
    "sweater_below": 60.0,
    "shorts_min": 75.0,
    "layer_swing": 20.0,
    "rain_chance": 50,
    "uv_min": 3.0,
    "soon_hours": 3,
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


class Config(NamedTuple):
    lat: float
    lon: float
    comfort: dict
    clothing: dict
    notify: dict


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

    # The notify keys used to live under [comfort]; still honored there.
    comfort_raw = raw.get("comfort", {})
    moved = {k: v for k, v in comfort_raw.items() if k in NOTIFY_DEFAULTS}
    comfort = {**DEFAULTS, **{k: v for k, v in comfort_raw.items() if k not in NOTIFY_DEFAULTS}}
    notify = {**NOTIFY_DEFAULTS, **moved, **raw.get("notify", {})}
    quiet = [notify["quiet_start"], notify["quiet_end"]]
    if any(h is not None for h in quiet) and not all(isinstance(h, int) and 0 <= h <= 23 for h in quiet):
        fail("set both quiet_start and quiet_end in [notify] to whole hours from 0 to 23.")
    clothing = {**CLOTHING_DEFAULTS, **raw.get("clothing", {})}
    return Config(float(location["lat"]), float(location["lon"]), comfort, clothing, notify)


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


def upcoming_changes(forecast, mode, c, lookahead):
    """Return (time, mode, temp, dew) for each hour in the lookahead where the mode changes."""
    hourly = forecast["hourly"]
    now = forecast["current"]["time"]
    hours = [
        (t, round(temp), round(dew))
        for t, temp, dew in zip(hourly["time"], hourly["temperature_2m"], hourly["dew_point_2m"])
        if t > now and temp is not None and dew is not None
    ][: int(lookahead)]

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


def notification(temp, dew, mode, changes, c, unit, lookahead):
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
        parts.append(f"no change for {lookahead}h")
    return title, " · ".join(parts)


class Outfit(NamedTuple):
    tier: str
    rain: tuple | None  # (time, chance %) at the peak chance of rain in the next few hours
    sun: tuple | None  # (time, UV index) at the peak UV in the next few hours
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
    # Rain and sun look only at the current hour plus the next soon_hours.
    soon = int(o["soon_hours"]) + 1
    rain = peak(hourly_window(forecast, "precipitation_probability", soon), o["rain_chance"])
    sun = peak(hourly_window(forecast, "uv_index", soon), o["uv_min"])
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
    lines = [f"Now: {degrees(temp, 'F')}, dew point {degrees(dew, 'F')} -> {ACTIONS[mode]}"]
    if changes:
        lines.append(f"Changes in the next {lookahead}h:")
        for t, m, t_temp, t_dew in changes:
            lines.append(
                f"  {format_time(t)}: {ACTIONS[m]} ({degrees(t_temp, 'F')}, dew {degrees(t_dew, 'F')})"
            )
    else:
        lines.append(f"No changes in the next {lookahead}h.")
    return "\n".join(lines)


class State(NamedTuple):
    mode: str
    outfit: str  # Outfit.key
    pushed_at: int  # epoch seconds of the last push


def read_state():
    """Return the saved State, or None if there is none or it can't be read."""
    try:
        return State(**json.loads(STATE_PATH.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def write_state(state):
    STATE_PATH.write_text(json.dumps(state._asdict()) + "\n")


def in_quiet_hours(hour, n):
    """True if hour (0-23, local) falls in [quiet_start, quiet_end), which may wrap past midnight."""
    start, end = n["quiet_start"], n["quiet_end"]
    if start is None or start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def worth_pushing(state, mode, outfit):
    """Mode and tier changes push; rain and sunscreen only when they turn on.

    Dropping them ("sunscreen off at 6 PM") isn't news, but it still updates the saved
    state so they push again next time they turn on.
    """
    if state is None:
        return True
    last_tier, *last_gear = state.outfit.split("+")
    gear = outfit.key.split("+")[1:]
    return mode != state.mode or outfit.tier != last_tier or bool(set(gear) - set(last_gear))


def hold_reasons(state, now, local_hour, n):
    """Why a due push should wait: quiet hours and/or the cooldown since the last push."""
    reasons = []
    if in_quiet_hours(local_hour, n):
        reasons.append("quiet hours")
    if state and 0 <= now - state.pushed_at < n["push_cooldown_hours"] * 3600:
        reasons.append(f"last push was under {n['push_cooldown_hours']}h ago")
    return reasons


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


def build_notifications(topic, temp, dew, mode, changes, outfit, cfg):
    """Return {ntfy topic: (title, body)}: °F to the topic, °C to topic-Metric."""
    notifications = {}
    for dest, unit in ((topic, "F"), (f"{topic}-Metric", "C")):
        title, body = notification(temp, dew, mode, changes, cfg.comfort, unit, cfg.notify["lookahead_hours"])
        notifications[dest] = (title, "\n".join([body, *wear_lines(outfit, unit)]))
    return notifications


def deliver(notifications, mode, outfit, local_hour, n):
    """Push if there's news and nothing holds it back; keep the saved state current."""
    state = read_state()
    now = time.time()
    label = f"mode {mode}, outfit {outfit.key}"
    if not worth_pushing(state, mode, outfit):
        # Keep the state current (e.g. rain ending) without resetting the cooldown.
        if (mode, outfit.key) != state[:2]:
            write_state(State(mode, outfit.key, state.pushed_at))
        print(f"No push ({label}).")
        return
    # A held push leaves the state alone, so a flip back to what was last pushed sends nothing.
    reasons = hold_reasons(state, now, local_hour, n)
    if reasons:
        print(f"Holding the push ({label}): {' and '.join(reasons)}.")
        return
    for dest, (title, body) in notifications.items():
        push(dest, title, body)
    write_state(State(mode, outfit.key, int(now)))
    print(f"Pushed to ntfy ({label}).")


def main():
    cfg = load_config()
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        fail("NTFY_TOPIC is not set; export it to your ntfy topic name.")

    forecast = fetch_forecast(cfg.lat, cfg.lon)
    current = forecast["current"]
    lookahead = cfg.notify["lookahead_hours"]
    # Classify on whole degrees so the decision matches the rounded values shown.
    temp, dew = round(current["temperature_2m"]), round(current["dew_point_2m"])
    mode = classify(temp, dew, cfg.comfort)
    changes = upcoming_changes(forecast, mode, cfg.comfort, lookahead)
    outfit = outfit_advice(forecast, cfg.clothing, lookahead)
    print(build_summary(temp, dew, mode, changes, lookahead))
    print("\n" + "\n".join(wear_lines(outfit, "F")))

    notifications = build_notifications(topic, temp, dew, mode, changes, outfit, cfg)
    for title, body in notifications.values():
        print(f"\n{title}\n{body}")

    local_hour = datetime.fromisoformat(current["time"]).hour
    deliver(notifications, mode, outfit, local_hour, cfg.notify)


if __name__ == "__main__":
    main()
