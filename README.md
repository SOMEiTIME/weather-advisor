# weather-advisor

A small script that checks the outdoor forecast and tells you whether to open the
windows, use fans, run the AC, or run the heat. When the recommendation changes,
it pushes a notification to your phone through [ntfy](https://ntfy.sh).

Weather comes from [Open-Meteo](https://open-meteo.com).

## Modes

Conditions are checked in this order; the first match wins. Temperature and dew
point are rounded to whole degrees first, so the numbers shown always match the
decision.

| Mode      | Condition                                            | Notification     | Recommendation (console)            |
|-----------|------------------------------------------------------|------------------|-------------------------------------|
| `heat`    | temp < `cold`                                        | 🔥 Heat on       | Close windows, run the heat         |
| `ac`      | temp > `hot` or dew point > `muggy_dew`              | ❄️ AC on         | Close windows, run the AC           |
| `windows` | `open_min` <= temp <= `open_max` and dew < `dry_dew` | 🪟 Open windows  | Open the windows                    |
| `crack`   | `crack_min` <= temp < `open_min` and dew < `dry_dew` | 🪟 Open windows a little | Open the windows a little     |
| `fan`     | temp > `open_max`                                    | 🌀 Fans on       | Use fans                            |
| `closed`  | anything else                                        | 🏠 Close up      | Keep windows closed, no HVAC needed |

## What to wear

The script also suggests an outfit, judged on the "feels like" temperature
(wind chill and humidity included), rounded to whole degrees:

| Feels like | Wear                              |
|------------|-----------------------------------|
| < `too_cold` (0°F) | way too cold, stay in if you can |
| < `hat_gloves_below` (35°F) | pants, coat, hat, gloves |
| < `coat_below` (45°F) | pants, coat                |
| < `sweater_below` (60°F) | pants, sweater          |
| < `shorts_min` (75°F) | pants, t-shirt             |
| otherwise  | shorts, t-shirt                   |

If feels-like swings by `layer_swing` (20°F) or more within the lookahead, it adds
a layering line, e.g. `Layer up: feels like 38°F now, 62°F at 3 PM`.

If the chance of rain reaches `rain_chance` (50%) at any hour in the lookahead, the
outfit adds a rain jacket at the peak hour, e.g. `Wear: pants, coat · rain jacket (70%
at 3 PM)`.

If the UV index reaches `uv_min` (3) at any hour in the lookahead, it adds sunscreen
at the peak hour, e.g. `sunscreen (UV 7 at 1 PM)`. It's never flagged overnight,
when UV is 0.

Rain or sunscreen turning on triggers a push; turning off does not (see State).

The outfit
shows on the console and as a second line of each notification, and a change of outfit
tier triggers a push (see State). These settings
live in a `[clothing]` section of `config.toml`; any you leave out use the defaults.

## Notifications

Each notification is short: the mode as the title, then a one-line body with the
current temperature and the next two changes in the lookahead window.

```
🪟 Open windows
69°F now · close up at 7 PM · heat on at 11 PM
```

The dew point only appears when it decided the mode: "muggy" when it triggers
the AC, "humid" when it keeps the windows closed at an otherwise good
temperature (at or above `crack_min`).

```
❄️ AC on
72°F now, muggy (dew 67°F) · fans on at 9 PM
```

The same notification goes to your topic in °F and to your topic plus `-Metric`
in °C. The console (and your cron log) still prints the full hour-by-hour
timeline with dew points.

## Requirements

- Python 3.11+ (uses `tomllib`)
- Standard library only, no packages to install
- Network access to `api.open-meteo.com` and `ntfy.sh`

Runs the same on a laptop, a Raspberry Pi, or a CI runner.

## Setup

1. Copy the example config and fill in your coordinates:

   ```sh
   cp config.example.toml config.toml
   ```

   `config.toml` is gitignored, so your location and tuned thresholds stay local.

2. Install the ntfy app on your phone and subscribe to a topic. Pick a long,
   hard-to-guess topic name, since anyone who knows it can read your messages.
   Each notification goes out twice: in °F to your topic, and in °C to the same
   topic with `-Metric` appended (e.g. `your-topic-name-Metric`). Subscribe to
   whichever you want, or both.

3. Set the topic in your environment (it is never read from a file):

   ```sh
   export NTFY_TOPIC=your-topic-name
   ```

4. Run it:

   ```sh
   python3 advisor.py
   ```

## Config reference

`config.toml` lives next to `advisor.py`.

| Key                          | Default  | Meaning                                                  |
|------------------------------|----------|----------------------------------------------------------|
| `[location] lat`             | required | Latitude in decimal degrees                              |
| `[location] lon`             | required | Longitude in decimal degrees                             |
| `[comfort] cold`             | 55       | Below this (°F): run the heat                            |
| `[comfort] crack_min`        | 55       | Lowest temp (°F) for opening windows a little            |
| `[comfort] open_min`         | 60       | Lowest temp (°F) for open windows                        |
| `[comfort] open_max`         | 75       | Highest temp (°F) for open windows; above it, use fans   |
| `[comfort] hot`              | 82       | Above this (°F): run the AC                              |
| `[comfort] dry_dew`          | 60       | Dew point (°F) must be below this to open windows        |
| `[comfort] muggy_dew`        | 65       | Above this dew point (°F): run the AC                    |
| `[comfort] lookahead_hours`  | 12       | Hours of forecast to scan for upcoming changes           |

Missing `[comfort]` keys fall back to the defaults. If `lat` or `lon` is missing,
the script exits and tells you to set up `config.toml`.

## Running on a schedule

Example crontab entry that checks every 30 minutes:

```cron
*/30 * * * * NTFY_TOPIC=your-topic-name /usr/bin/python3 /path/to/weather-advisor/advisor.py >> /tmp/weather-advisor.log 2>&1
```

## State

The last window mode and outfit are saved in `~/.weather_advisor_state` (e.g.
`windows|cool+rain+sun`). The script always prints the summary, but only pushes to ntfy
when:

- the window mode changes,
- the outfit tier changes, or
- rain or sunscreen turns on.

Rain or sunscreen turning off is not news ("sunscreen off at 6 PM"), so it doesn't
push, but the file is still updated so the next time it turns on, it pushes again. The
layering note's numbers don't count, so a shifting feels-like within a tier doesn't
re-push. Delete the file to force a push on the next run.

A fresh CI runner has no state file, so it will push on every run.

## Future ideas

- GitHub Actions workflow on a schedule (with the location and topic as secrets)
- Home Assistant integration
- Pi-hole / Raspberry Pi status display
- Alexa announcements
