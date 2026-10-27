# weather-advisor

Checks the outdoor forecast and tells you whether to open the windows, run fans,
the AC or the heat, and what to wear. Pushes to your phone through
[ntfy](https://ntfy.sh) when something changes. Weather comes from
[Open-Meteo](https://open-meteo.com).

Needs Python 3.11+ and nothing else. It's one file, `advisor.py`.

## Setup

1. `cp config.example.toml config.toml` and put in your lat/lon. `config.toml` is
   gitignored.
2. Subscribe to an ntfy topic in the app. Make the name long and hard to guess,
   since anyone who knows it can read it. Every push goes to the topic in °F and to
   `<topic>-Metric` in °C.
3. `export NTFY_TOPIC=your-topic-name` (it's never read from a file).
4. `python3 advisor.py`

To run it on a schedule:

```cron
*/30 * * * * NTFY_TOPIC=your-topic-name /usr/bin/python3 /path/to/weather-advisor/advisor.py >> /tmp/weather-advisor.log 2>&1
```

## Windows and HVAC

Temp and dew point are rounded to whole degrees first, so the numbers shown match
the decision. First match wins:

| Mode      | Condition                                            | Notification             |
|-----------|------------------------------------------------------|--------------------------|
| `heat`    | temp < `cold`                                        | 🔥 Heat on               |
| `ac`      | temp > `hot` or dew point > `muggy_dew`              | ❄️ AC on                 |
| `windows` | `open_min` <= temp <= `open_max` and dew < `dry_dew` | 🪟 Open windows          |
| `crack`   | `crack_min` <= temp < `open_min` and dew < `dry_dew` | 🪟 Open windows a little |
| `fan`     | temp > `open_max`                                    | 🌀 Fans on               |
| `closed`  | anything else                                        | 🏠 Close up              |

A notification is a title plus a short body: the current temp, the next two
changes within the lookahead, and the outfit.

```
🪟 Open windows
69°F now · close up at 7 PM · heat on at 11 PM
Wear: pants, t-shirt
```

The dew point only shows when it decided the mode: "muggy" when it forces the AC,
"humid" when it keeps the windows shut at an otherwise fine temperature.

The console output also has the full list of changes with dew points.

## What to wear

Based on the feels-like temp, rounded:

| Feels like                  | Wear                             |
|-----------------------------|----------------------------------|
| < `too_cold` (0°F)          | way too cold, stay in if you can |
| < `hat_gloves_below` (35°F) | pants, coat, hat, gloves         |
| < `coat_below` (45°F)       | pants, coat                      |
| < `sweater_below` (60°F)    | pants, sweater                   |
| < `shorts_min` (75°F)       | pants, t-shirt                   |
| otherwise                   | shorts, t-shirt                  |

Extras:

- **Layering:** if feels-like swings by `layer_swing` (20°F) or more within the
  lookahead, you get `Layer up: feels like 38°F now, 62°F at 3 PM`.
- **Rain jacket:** rain chance hits `rain_chance` (50%) within the next `soon_hours`
  (3). Shows the peak hour.
- **Sunscreen:** UV hits `uv_min` (3) within the next `soon_hours`. Shows the peak
  hour.

Rain and sunscreen only look a few hours out, so a far-off forecast doesn't
trigger them early.

## Config

`config.toml` sits next to `advisor.py`. Anything you leave out uses the default.

| Key                              | Default  | Meaning                                         |
|----------------------------------|----------|-------------------------------------------------|
| `[location] lat`, `lon`          | required | Decimal degrees                                 |
| `[comfort] cold`                 | 55       | Below this (°F): heat                           |
| `[comfort] crack_min`            | 55       | Lowest temp for cracking the windows            |
| `[comfort] open_min`             | 60       | Lowest temp for open windows                    |
| `[comfort] open_max`             | 75       | Highest temp for open windows; above it, fans   |
| `[comfort] hot`                  | 82       | Above this: AC                                  |
| `[comfort] dry_dew`              | 60       | Dew point must be under this to open windows    |
| `[comfort] muggy_dew`            | 65       | Dew point above this: AC                        |
| `[notify] lookahead_hours`       | 12       | How far ahead to look for changes               |
| `[notify] push_cooldown_hours`   | 4        | Minimum hours between pushes                    |
| `[notify] quiet_start`/`quiet_end` | off    | Local hours (0-23) to hold pushes between       |
| `[clothing] ...`                 |          | The thresholds above; see `config.example.toml` |

The `[notify]` keys used to live under `[comfort]`. They still work there.

## When it pushes

The last push is saved in `~/.weather_advisor_state.json` (mode, outfit, time).
It pushes when:

- the mode changes,
- the outfit tier changes, or
- rain or sunscreen turns on.

Rain or sunscreen turning off isn't news, so no push, but the file is updated so
they push again next time. Shifting feels-like numbers within a tier don't count.
Delete the file to force a push. An unreadable file (including the old
pipe-separated format) counts as no file, so you get one extra push.

A due push waits during the cooldown and during quiet hours (if set). The saved
state stays put while it waits, so if things flip back to what you were last told,
nothing is sent. Otherwise the current state goes out when the hold ends. Quiet hours
use the forecast's local time, not the machine's.

## Tests

```sh
python3 -m unittest
```
