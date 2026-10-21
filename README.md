# weather-advisor

A small script that checks the outdoor forecast and tells you whether to open the
windows, use fans, run the AC, or run the heat. When the recommendation changes,
it pushes a notification to your phone through [ntfy](https://ntfy.sh).

Weather comes from [Open-Meteo](https://open-meteo.com).

## Modes

Conditions are checked in this order; the first match wins.

| Mode      | Condition                                          | Recommendation                       |
|-----------|----------------------------------------------------|--------------------------------------|
| `heat`    | temp < `cold`                                      | Close windows, run the heat          |
| `ac`      | temp > `hot` or dew point > `muggy_dew`            | Close windows, run the AC            |
| `windows` | `open_min` <= temp <= `open_max` and dew < `dry_dew` | Open the windows                   |
| `fan`     | temp > `open_max`                                  | Use fans                             |
| `closed`  | anything else                                      | Keep windows closed, no HVAC needed  |

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

The last mode is saved in `~/.weather_advisor_state`. The script always prints the
summary, but only pushes to ntfy when the current mode differs from the saved
one, then updates the file. Delete the file to force a push on the next run.

A fresh CI runner has no state file, so it will push on every run.

## Future ideas

- GitHub Actions workflow on a schedule (with the location and topic as secrets)
- Home Assistant integration
- Pi-hole / Raspberry Pi status display
- Alexa announcements
