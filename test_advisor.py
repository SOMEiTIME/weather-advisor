import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import advisor

C = advisor.DEFAULTS


def forecast(current_temp, current_dew, hours):
    """Build a fake Open-Meteo response; hours is a list of (temp, dew) starting at 13:00."""
    return {
        "current": {"time": "2026-10-21T12:00", "temperature_2m": current_temp, "dew_point_2m": current_dew},
        "hourly": {
            "time": [f"2026-10-21T{13 + i:02d}:00" for i in range(len(hours))],
            "temperature_2m": [t for t, _ in hours],
            "dew_point_2m": [d for _, d in hours],
        },
    }


class ClassifyTest(unittest.TestCase):
    def test_modes_and_boundaries(self):
        cases = [
            (54, 40, "heat"),
            (55, 40, "closed"),
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
            [("15:00", "closed", 59), ("16:00", "heat", 54)],
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


class ConfigTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            path.write_text(text)
            with mock.patch.object(advisor, "CONFIG_PATH", path):
                return advisor.load_config()

    def test_missing_comfort_keys_use_defaults(self):
        lat, lon, comfort = self.load("[location]\nlat = 1.5\nlon = -2.5\n[comfort]\nhot = 90\n")
        self.assertEqual((lat, lon), (1.5, -2.5))
        self.assertEqual(comfort["hot"], 90)
        self.assertEqual(comfort["cold"], C["cold"])

    def test_missing_lat_lon_exits_with_setup_hint(self):
        with self.assertRaises(SystemExit) as cm:
            self.load("[location]\nlat = 1.5\n")
        self.assertIn("lon", str(cm.exception))
        self.assertIn("config.example.toml", str(cm.exception))


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        for patch in (
            mock.patch.object(advisor, "STATE_PATH", self.state),
            mock.patch.object(advisor, "load_config", return_value=(0.0, 0.0, C)),
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
            [("test-topic", "Open the windows"), ("test-topic-Metric", "Open the windows")],
        )
        self.assertIn("Now: 60°F", push.call_args_list[0].args[2])
        self.assertIn("Now: 16°C", push.call_args_list[1].args[2])
        self.assertIn("Now: 60°F", out)
        self.assertEqual(self.state.read_text().strip(), "windows")

        push, out = self.run_main()
        push.assert_not_called()
        self.assertIn("no push", out)

    def test_missing_topic_exits(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as cm:
            advisor.main()
        self.assertIn("NTFY_TOPIC", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
