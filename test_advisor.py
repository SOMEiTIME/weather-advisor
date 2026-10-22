import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import advisor

C = advisor.DEFAULTS
N = advisor.NOTIFY_DEFAULTS
O = advisor.CLOTHING_DEFAULTS


def forecast(current_temp, current_dew, hours, current_feels=None, feels=None, rain=None, uv=None, now="2026-10-21T12:00"):
    """Build a fake Open-Meteo response; hours is a list of (temp, dew) starting at 13:00.

    Feels-like defaults to the air temperature; the rain chance and UV default to zero.
    """
    return {
        "current": {
            "time": now,
            "temperature_2m": current_temp,
            "dew_point_2m": current_dew,
            "apparent_temperature": current_temp if current_feels is None else current_feels,
        },
        "hourly": {
            "time": [f"2026-10-21T{13 + i:02d}:00" for i in range(len(hours))],
            "temperature_2m": [t for t, _ in hours],
            "dew_point_2m": [d for _, d in hours],
            "apparent_temperature": feels if feels is not None else [t for t, _ in hours],
            "precipitation_probability": rain if rain is not None else [0] * len(hours),
            "uv_index": uv if uv is not None else [0] * len(hours),
        },
    }


class ClassifyTest(unittest.TestCase):
    def test_modes_and_boundaries(self):
        cases = [
            (54, 40, "heat"),
            (55, 40, "crack"),
            (59, 40, "crack"),
            (59, 60, "closed"),
            (60, 40, "windows"),
            (75, 59, "windows"),
            (70, 60, "closed"),
            (70, 66, "ac"),
            (76, 40, "fan"),
            (82, 40, "fan"),
            (83, 40, "ac"),
        ]
        for temp, dew, expected in cases:
            with self.subTest(temp=temp, dew=dew):
                self.assertEqual(advisor.classify(temp, dew, C), expected)


class UpcomingChangesTest(unittest.TestCase):
    def test_lists_only_changes_with_rounded_values(self):
        f = forecast(70, 40, [(70, 40), (59.6, 41), (59.4, 41), (54.5, 41), (54, 41)])
        changes = advisor.upcoming_changes(f, "windows", C, 12)
        self.assertEqual(
            [(t[-5:], m, temp) for t, m, temp, _ in changes],
            [("15:00", "crack", 59), ("16:00", "heat", 54)],
        )

    def test_respects_lookahead_and_skips_missing(self):
        f = forecast(70, 40, [(None, 40), (70, 40), (50, 40)])
        self.assertEqual(advisor.upcoming_changes(f, "windows", C, 1), [])


class FormatTimeTest(unittest.TestCase):
    def test_twelve_hour_clock(self):
        self.assertEqual(advisor.format_time("2026-10-21T00:00"), "Wed 12 AM")
        self.assertEqual(advisor.format_time("2026-10-21T19:00"), "Wed 7 PM")


class DegreesTest(unittest.TestCase):
    def test_fahrenheit_and_celsius(self):
        self.assertEqual(advisor.degrees(69, "F"), "69°F")
        self.assertEqual(advisor.degrees(32, "C"), "0°C")
        self.assertEqual(advisor.degrees(69, "C"), "21°C")
        self.assertEqual(advisor.degrees(14, "C"), "-10°C")


class NotificationTest(unittest.TestCase):
    def test_shows_next_two_changes(self):
        changes = [
            ("2026-10-21T19:00", "crack", 59, 41),
            ("2026-10-21T23:00", "heat", 54, 41),
            ("2026-10-22T02:00", "closed", 55, 41),
        ]
        self.assertEqual(
            advisor.notification(69, 43, "windows", changes, C, "F", 12),
            ("🪟 Open windows", "69°F now · open windows a little at 7 PM · heat on at 11 PM"),
        )
        self.assertEqual(
            advisor.notification(69, 43, "windows", changes, C, "C", 12)[1],
            "21°C now · open windows a little at 7 PM · heat on at 11 PM",
        )

    def test_dew_point_only_when_it_decides(self):
        self.assertEqual(
            advisor.notification(72, 67, "ac", [("2026-10-21T21:00", "fan", 77, 60)], C, "F", 12),
            ("❄️ AC on", "72°F now, muggy (dew 67°F) · fans on at 9 PM"),
        )
        self.assertEqual(
            advisor.notification(70, 62, "closed", [], C, "F", 12),
            ("🏠 Close up", "70°F now, humid (dew 62°F) · no change for 12h"),
        )
        self.assertEqual(
            advisor.notification(78, 50, "fan", [("2026-10-22T09:00", "ac", 70, 66)], C, "F", 12)[1],
            "78°F now · AC on at 9 AM (dew 66°F)",
        )
        self.assertEqual(advisor.notification(50, 30, "heat", [], C, "F", 12)[0], "🔥 Heat on")


class OutfitTierTest(unittest.TestCase):
    def test_tiers_and_boundaries(self):
        cases = [
            (-1, "frigid"),
            (0, "freezing"),
            (34, "freezing"),
            (35, "cold"),
            (44, "cold"),
            (45, "cool"),
            (59, "cool"),
            (60, "mild"),
            (74, "mild"),
            (75, "warm"),
        ]
        for feels, expected in cases:
            with self.subTest(feels=feels):
                self.assertEqual(advisor.outfit_tier(feels, O), expected)


class OutfitAdviceTest(unittest.TestCase):
    def advice(self, **kwargs):
        hours = [(60, 40)] * 6
        return advisor.outfit_advice(forecast(60, 40, hours, **kwargs), O, 12)

    def test_plain_day_has_no_extras(self):
        a = self.advice()
        self.assertEqual((a.tier, a.rain, a.sun, a.layers), ("mild", None, None, None))
        self.assertEqual(a.key, "mild")
        self.assertEqual(advisor.wear_lines(a, "F"), ["Wear: pants, t-shirt"])

    def test_rain_uses_the_peak_hour(self):
        a = self.advice(rain=[10, 40, 70, 60, 0, 0])
        self.assertEqual(a.rain, ("2026-10-21T15:00", 70))
        self.assertEqual(a.key, "mild+rain")
        self.assertEqual(advisor.wear_lines(a, "F"), ["Wear: pants, t-shirt · rain jacket (70% at 3 PM)"])

    def test_rain_and_sun_only_count_soon(self):
        # The window is the current hour plus the next 3 (here 13:00-16:00); 17:00 is too far out.
        a = self.advice(rain=[0, 0, 0, 0, 90, 0], uv=[0, 0, 0, 0, 8, 0])
        self.assertEqual((a.rain, a.sun, a.key), (None, None, "mild"))
        a = self.advice(rain=[0, 0, 0, 90, 0, 0], uv=[0, 0, 0, 8, 0, 0])
        self.assertEqual(a.key, "mild+rain+sun")

    def test_below_threshold_or_missing_data_is_left_out(self):
        self.assertIsNone(self.advice(rain=[49] * 6).rain)
        self.assertIsNone(self.advice(rain=[None] * 6).rain)
        self.assertEqual(self.advice(rain=[None, None, 60, None, None, None]).rain, ("2026-10-21T15:00", 60))
        self.assertIsNone(self.advice(uv=[2.9] * 6).sun)
        self.assertIsNone(self.advice(uv=[None] * 6).sun)

    def test_sunscreen_uses_the_peak_uv(self):
        a = self.advice(uv=[0, 2, 6, 7.4, 3, 0])
        self.assertEqual(a.sun, ("2026-10-21T16:00", 7))
        self.assertEqual(a.key, "mild+sun")
        self.assertEqual(advisor.wear_lines(a, "F"), ["Wear: pants, t-shirt · sunscreen (UV 7 at 4 PM)"])

    def test_layering_when_feels_like_swings(self):
        a = self.advice(current_feels=38, feels=[40, 50, 62, 58, 50, 45])
        self.assertEqual(a.tier, "cold")
        self.assertEqual(a.layers, (("now", 38), ("3 PM", 62)))
        self.assertEqual(
            advisor.wear_lines(a, "F"),
            ["Wear: pants, coat", "Layer up: feels like 38°F now, 62°F at 3 PM"],
        )
        self.assertEqual(advisor.wear_lines(a, "C")[1], "Layer up: feels like 3°C now, 17°C at 3 PM")

    def test_layering_orders_points_by_time(self):
        a = self.advice(current_feels=60, feels=[50, 40, 45, 50, 55, 58])
        self.assertEqual(a.layers, (("now", 60), ("2 PM", 40)))

    def test_small_swing_is_not_layering(self):
        self.assertIsNone(self.advice(current_feels=60, feels=[61, 65, 70, 75, 78, 79]).layers)


class QuietHoursTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(any(advisor.in_quiet_hours(h, N) for h in range(24)))

    def test_window_that_wraps_past_midnight(self):
        n = {**N, "quiet_start": 22, "quiet_end": 6}
        quiet = [h for h in range(24) if advisor.in_quiet_hours(h, n)]
        self.assertEqual(quiet, [0, 1, 2, 3, 4, 5, 22, 23])

    def test_window_within_a_day(self):
        n = {**N, "quiet_start": 13, "quiet_end": 15}
        self.assertEqual([h for h in range(24) if advisor.in_quiet_hours(h, n)], [13, 14])


class ConfigTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text)
            with mock.patch.object(advisor, "CONFIG_PATH", path):
                return advisor.load_config()

    def test_missing_comfort_keys_use_defaults(self):
        cfg = self.load("[location]\nlat = 1.5\nlon = -2.5\n[comfort]\nhot = 90\n")
        self.assertEqual((cfg.lat, cfg.lon), (1.5, -2.5))
        self.assertEqual(cfg.comfort, {**C, "hot": 90})
        self.assertEqual(cfg.clothing, O)
        self.assertEqual(cfg.notify, N)

    def test_clothing_overrides(self):
        clothing = self.load("[location]\nlat = 1\nlon = 2\n[clothing]\ncoat_below = 50\n").clothing
        self.assertEqual(clothing, {**O, "coat_below": 50})

    def test_notify_section_and_old_comfort_location(self):
        head = "[location]\nlat = 1\nlon = 2\n"
        new = self.load(head + "[notify]\nlookahead_hours = 6\npush_cooldown_hours = 1\n")
        self.assertEqual(new.notify, {**N, "lookahead_hours": 6, "push_cooldown_hours": 1})
        old = self.load(head + "[comfort]\nlookahead_hours = 6\nhot = 90\n")
        self.assertEqual(old.notify["lookahead_hours"], 6)
        self.assertEqual(old.comfort, {**C, "hot": 90})

    def test_quiet_hours_need_both_ends_as_hours(self):
        head = "[location]\nlat = 1\nlon = 2\n[notify]\n"
        self.assertEqual(self.load(head + "quiet_start = 22\nquiet_end = 6\n").notify["quiet_start"], 22)
        for bad in ("quiet_start = 22\n", "quiet_end = 6\n", "quiet_start = 24\nquiet_end = 6\n", "quiet_start = 22\nquiet_end = 6.5\n"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as cm:
                self.load(head + bad)
            self.assertIn("quiet_start and quiet_end", str(cm.exception))

    def test_missing_lat_lon_exits_with_setup_hint(self):
        with self.assertRaises(SystemExit) as cm:
            self.load("[location]\nlat = 1.5\n")
        self.assertIn("lon", str(cm.exception))
        self.assertIn("config.example.toml", str(cm.exception))


class PushTest(unittest.TestCase):
    def test_sends_utf8_json(self):
        with mock.patch.object(advisor.urllib.request, "urlopen") as urlopen:
            advisor.push("test-topic", "🪟 Open windows", "21°C now")
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://ntfy.sh")
        self.assertEqual(
            json.loads(req.data.decode("utf-8")),
            {"topic": "test-topic", "title": "🪟 Open windows", "message": "21°C now"},
        )
        # Every header must be sendable (http.client encodes them as Latin-1).
        for value in req.header_items():
            "".join(value).encode("latin-1")


CFG = advisor.Config(0.0, 0.0, C, O, N)


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        for patch in (
            mock.patch.object(advisor, "STATE_PATH", self.state),
            mock.patch.object(advisor, "load_config", return_value=CFG),
            mock.patch.object(advisor, "fetch_forecast", return_value=forecast(59.6, 40, [])),
            mock.patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}),
            mock.patch.object(advisor, "time", mock.Mock(time=lambda: self.now)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.now = 1_000_000

    def saved(self):
        return json.loads(self.state.read_text())

    def run_main(self, hours_later=0):
        self.now += hours_later * 3600
        with mock.patch.object(advisor, "push") as push, redirect_stdout(io.StringIO()) as out:
            advisor.main()
        return push, out.getvalue()

    def test_pushes_only_when_mode_changes(self):
        push, out = self.run_main()
        self.assertEqual(
            [c.args[:2] for c in push.call_args_list],
            [("test-topic", "🪟 Open windows"), ("test-topic-Metric", "🪟 Open windows")],
        )
        self.assertEqual(push.call_args_list[0].args[2], "60°F now · no change for 12h\nWear: pants, t-shirt")
        self.assertEqual(push.call_args_list[1].args[2], "16°C now · no change for 12h\nWear: pants, t-shirt")
        self.assertIn("Now: 60°F", out)
        self.assertIn("Wear: pants, t-shirt", out)
        self.assertEqual(self.saved(), {"mode": "windows", "outfit": "mild", "pushed_at": 1_000_000})

        push, out = self.run_main()
        push.assert_not_called()
        self.assertIn("No push", out)

    def test_pushes_when_only_the_outfit_changes(self):
        self.run_main()
        cooler = forecast(59.6, 40, [], current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=cooler):
            push, _ = self.run_main(hours_later=4)
        self.assertEqual(push.call_count, 2)
        self.assertIn("Wear: pants, sweater", push.call_args_list[0].args[2])
        self.assertEqual(self.saved(), {"mode": "windows", "outfit": "cool", "pushed_at": 1_014_400})

    def test_rain_and_sunscreen_push_only_when_they_turn_on(self):
        def run_with(**kwargs):
            wx = forecast(59.6, 40, [(60, 40)] * 3, **kwargs)
            with mock.patch.object(advisor, "fetch_forecast", return_value=wx):
                return self.run_main(hours_later=4)[0]

        run_with(rain=[0, 80, 0], uv=[0, 6, 0])
        self.assertEqual(self.saved()["outfit"], "mild+rain+sun")

        # Rain and sunscreen ending: no push, but the saved state follows (and keeps the push time).
        push = run_with()
        push.assert_not_called()
        self.assertEqual(self.saved(), {"mode": "windows", "outfit": "mild", "pushed_at": 1_014_400})

        # Back on: push again, once per kind of gear added.
        self.assertEqual(run_with(uv=[0, 6, 0]).call_count, 2)
        self.assertEqual(run_with(rain=[0, 80, 0], uv=[0, 6, 0]).call_count, 2)

    def test_dropping_rain_while_the_tier_changes_still_pushes(self):
        wet = forecast(59.6, 40, [(60, 40)] * 3, rain=[0, 80, 0])
        with mock.patch.object(advisor, "fetch_forecast", return_value=wet):
            self.run_main()
        colder = forecast(59.6, 40, [(60, 40)] * 3, current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=colder):
            push, _ = self.run_main(hours_later=4)
        self.assertEqual(push.call_count, 2)

    def test_unreadable_state_pushes_once_and_is_rewritten(self):
        for junk in ("windows|mild|1000000\n", "{not json", '{"mode": "windows"}'):
            with self.subTest(junk=junk):
                self.state.write_text(junk)
                push, _ = self.run_main()
                self.assertEqual(push.call_count, 2)
                self.assertEqual(self.saved()["pushed_at"], self.now)

    def test_cooldown_holds_a_due_push_then_sends_the_current_state(self):
        self.run_main()
        cooler = forecast(59.6, 40, [], current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=cooler):
            push, out = self.run_main(hours_later=1)
            push.assert_not_called()
            self.assertIn("Holding the push", out)
            # Held pushes leave the state alone, so the cooldown still counts from the last real push.
            self.assertEqual(self.saved()["pushed_at"], 1_000_000)
            for _ in range(2):  # 2h and 3h in: still held
                push, _ = self.run_main(hours_later=1)
                push.assert_not_called()
            push, _ = self.run_main(hours_later=1)  # 4h in: the cooldown is over
        self.assertEqual(push.call_count, 2)
        self.assertIn("Wear: pants, sweater", push.call_args_list[0].args[2])
        self.assertEqual(self.saved(), {"mode": "windows", "outfit": "cool", "pushed_at": 1_014_400})

    def test_a_flip_back_during_the_cooldown_sends_nothing(self):
        self.run_main()
        cooler = forecast(59.6, 40, [], current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=cooler):
            self.run_main(hours_later=1)  # held
        push, _ = self.run_main(hours_later=1)  # back to what was last pushed
        push.assert_not_called()
        push, _ = self.run_main(hours_later=5)
        push.assert_not_called()

    def test_quiet_hours_hold_a_push_until_they_end(self):
        quiet = CFG._replace(notify={**N, "quiet_start": 22, "quiet_end": 6})
        self.run_main()  # noon: pushes "windows|mild"
        cooler = lambda at: forecast(59.6, 40, [], current_feels=50, now=at)
        with mock.patch.object(advisor, "load_config", return_value=quiet):
            with mock.patch.object(advisor, "fetch_forecast", return_value=cooler("2026-10-21T23:00")):
                push, out = self.run_main(hours_later=11)
                push.assert_not_called()
                self.assertIn("quiet hours", out)
                self.assertEqual(self.saved()["pushed_at"], 1_000_000)
            with mock.patch.object(advisor, "fetch_forecast", return_value=cooler("2026-10-22T06:00")):
                push, _ = self.run_main(hours_later=7)
        self.assertEqual(push.call_count, 2)
        self.assertIn("Wear: pants, sweater", push.call_args_list[0].args[2])

    def test_quiet_hours_use_the_forecasts_local_time(self):
        quiet = CFG._replace(notify={**N, "quiet_start": 22, "quiet_end": 6})
        late = forecast(59.6, 40, [], now="2026-10-21T22:15")
        with mock.patch.object(advisor, "load_config", return_value=quiet):
            with mock.patch.object(advisor, "fetch_forecast", return_value=late):
                push, _ = self.run_main()
        push.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_cooldown_ignores_a_clock_that_went_backwards(self):
        self.run_main()
        cooler = forecast(59.6, 40, [], current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=cooler):
            push, _ = self.run_main(hours_later=-1)
        self.assertEqual(push.call_count, 2)

    def test_missing_topic_exits(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as cm:
            advisor.main()
        self.assertIn("NTFY_TOPIC", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
