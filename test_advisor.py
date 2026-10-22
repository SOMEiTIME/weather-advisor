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
O = advisor.CLOTHING_DEFAULTS


def forecast(current_temp, current_dew, hours, current_feels=None, feels=None, rain=None, uv=None):
    """Build a fake Open-Meteo response; hours is a list of (temp, dew) starting at 13:00.

    Feels-like defaults to the air temperature the rain chance and UV to zero.
    """
    return {
        "current": {
            "time": "2026-10-21T12:00",
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
        changes = advisor.upcoming_changes(f, "windows", C)
        self.assertEqual(
            [(t[-5:], m, temp) for t, m, temp, _ in changes],
            [("15:00", "crack", 59), ("16:00", "heat", 54)],
        )

    def test_respects_lookahead_and_skips_missing(self):
        f = forecast(70, 40, [(None, 40), (70, 40), (50, 40)])
        self.assertEqual(advisor.upcoming_changes(f, "windows", {**C, "lookahead_hours": 1}), [])


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
            advisor.notification(69, 43, "windows", changes, C, "F"),
            ("🪟 Open windows", "69°F now · open windows a little at 7 PM · heat on at 11 PM"),
        )
        self.assertEqual(
            advisor.notification(69, 43, "windows", changes, C, "C")[1],
            "21°C now · open windows a little at 7 PM · heat on at 11 PM",
        )

    def test_dew_point_only_when_it_decides(self):
        self.assertEqual(
            advisor.notification(72, 67, "ac", [("2026-10-21T21:00", "fan", 77, 60)], C, "F"),
            ("❄️ AC on", "72°F now, muggy (dew 67°F) · fans on at 9 PM"),
        )
        self.assertEqual(
            advisor.notification(70, 62, "closed", [], C, "F"),
            ("🏠 Close up", "70°F now, humid (dew 62°F) · no change for 12h"),
        )
        self.assertEqual(
            advisor.notification(78, 50, "fan", [("2026-10-22T09:00", "ac", 70, 66)], C, "F")[1],
            "78°F now · AC on at 9 AM (dew 66°F)",
        )
        self.assertEqual(advisor.notification(50, 30, "heat", [], C, "F")[0], "🔥 Heat on")


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

    def test_rain_below_threshold_or_missing_is_left_out(self):
        self.assertIsNone(self.advice(rain=[49] * 6).rain)
        self.assertEqual(self.advice(rain=[None, None, 60, None, None, None]).rain, ("2026-10-21T15:00", 60))
        self.assertIsNone(self.advice(rain=[None] * 6).rain)

    def test_sunscreen_uses_the_peak_uv(self):
        a = self.advice(uv=[0, 2, 6, 7.4, 3, 0])
        self.assertEqual(a.sun, ("2026-10-21T16:00", 7))
        self.assertEqual(a.key, "mild+sun")
        self.assertEqual(advisor.wear_lines(a, "F"), ["Wear: pants, t-shirt · sunscreen (UV 7 at 4 PM)"])

    def test_rain_and_sunscreen_together(self):
        a = self.advice(rain=[70, 0, 0, 0, 0, 0], uv=[0, 0, 5, 0, 0, 0])
        self.assertEqual(a.key, "mild+rain+sun")
        self.assertEqual(
            advisor.wear_lines(a, "F"),
            ["Wear: pants, t-shirt · rain jacket (70% at 1 PM) · sunscreen (UV 5 at 3 PM)"],
        )

    def test_uv_below_threshold_or_missing_is_left_out(self):
        self.assertIsNone(self.advice(uv=[2.9] * 6).sun)
        self.assertIsNone(self.advice(uv=[None] * 6).sun)

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


class ConfigTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text)
            with mock.patch.object(advisor, "CONFIG_PATH", path):
                return advisor.load_config()

    def test_missing_comfort_keys_use_defaults(self):
        lat, lon, comfort, clothing = self.load("[location]\nlat = 1.5\nlon = -2.5\n[comfort]\nhot = 90\n")
        self.assertEqual((lat, lon), (1.5, -2.5))
        self.assertEqual(comfort["hot"], 90)
        self.assertEqual(comfort["cold"], C["cold"])
        self.assertEqual(clothing, O)

    def test_clothing_overrides(self):
        *_, clothing = self.load("[location]\nlat = 1\nlon = 2\n[clothing]\ncoat_below = 50\n")
        self.assertEqual(clothing, {**O, "coat_below": 50})

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


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        for patch in (
            mock.patch.object(advisor, "STATE_PATH", self.state),
            mock.patch.object(advisor, "load_config", return_value=(0.0, 0.0, C, O)),
            mock.patch.object(advisor, "fetch_forecast", return_value=forecast(59.6, 40, [])),
            mock.patch.dict(os.environ, {"NTFY_TOPIC": "test-topic"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self):
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
        self.assertEqual(self.state.read_text().strip(), "windows|mild")

        push, out = self.run_main()
        push.assert_not_called()
        self.assertIn("No push", out)

    def test_pushes_when_only_the_outfit_changes(self):
        self.run_main()
        cooler = forecast(59.6, 40, [], current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=cooler):
            push, _ = self.run_main()
        self.assertEqual(push.call_count, 2)
        self.assertIn("Wear: pants, sweater", push.call_args_list[0].args[2])
        self.assertEqual(self.state.read_text().strip(), "windows|cool")

    def test_pushes_when_rain_is_added(self):
        self.run_main()
        wet = forecast(59.6, 40, [(60, 40)] * 3, rain=[0, 80, 0])
        with mock.patch.object(advisor, "fetch_forecast", return_value=wet):
            push, _ = self.run_main()
        self.assertEqual(push.call_count, 2)
        self.assertIn("rain jacket (80% at 2 PM)", push.call_args_list[0].args[2])
        self.assertEqual(self.state.read_text().strip(), "windows|mild+rain")

    def test_pushes_when_sunscreen_is_added(self):
        self.run_main()
        sunny = forecast(59.6, 40, [(60, 40)] * 3, uv=[0, 6, 0])
        with mock.patch.object(advisor, "fetch_forecast", return_value=sunny):
            push, _ = self.run_main()
        self.assertEqual(push.call_count, 2)
        self.assertIn("sunscreen (UV 6 at 2 PM)", push.call_args_list[0].args[2])
        self.assertEqual(self.state.read_text().strip(), "windows|mild+sun")

    def test_rain_and_sunscreen_push_only_when_they_turn_on(self):
        def run_with(**kwargs):
            wx = forecast(59.6, 40, [(60, 40)] * 3, **kwargs)
            with mock.patch.object(advisor, "fetch_forecast", return_value=wx):
                return self.run_main()[0]

        run_with(rain=[0, 80, 0], uv=[0, 6, 0])
        self.assertEqual(self.state.read_text().strip(), "windows|mild+rain+sun")

        # Rain and sunscreen ending: no push, but the saved state follows.
        push = run_with()
        push.assert_not_called()
        self.assertEqual(self.state.read_text().strip(), "windows|mild")

        # Back on: push again, once per kind of gear added.
        self.assertEqual(run_with(uv=[0, 6, 0]).call_count, 2)
        self.assertEqual(run_with(rain=[0, 80, 0], uv=[0, 6, 0]).call_count, 2)

    def test_dropping_rain_while_the_tier_changes_still_pushes(self):
        wet = forecast(59.6, 40, [(60, 40)] * 3, rain=[0, 80, 0])
        with mock.patch.object(advisor, "fetch_forecast", return_value=wet):
            self.run_main()
        colder = forecast(59.6, 40, [(60, 40)] * 3, current_feels=50)
        with mock.patch.object(advisor, "fetch_forecast", return_value=colder):
            push, _ = self.run_main()
        self.assertEqual(push.call_count, 2)

    def test_older_mode_only_state_pushes_once(self):
        self.state.write_text("windows\n")
        push, _ = self.run_main()
        self.assertEqual(push.call_count, 2)
        self.assertEqual(self.state.read_text().strip(), "windows|mild")

    def test_missing_topic_exits(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as cm:
            advisor.main()
        self.assertIn("NTFY_TOPIC", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
