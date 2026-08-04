import os
import unittest

from garminworkouts.config import configreader


class MyTestCase(unittest.TestCase):
    def test_read_config(self):
        config_file = os.path.join(os.path.dirname(__file__), "test_configreader.yaml")
        config = configreader.read_config(config_file)

        expected_config = {
            "name": "Test",
            "sport": "running",
            "steps": [
                [{"type": "warmup", "duration": "5:00", "heart_rate_max": 120}],
                [
                    {"type": "interval", "duration": "3:00", "pace": "5:25-5:30"},
                    {"type": "recovery", "duration": "1:30"},
                ],
                [
                    {"type": "interval", "duration": "3:00", "pace": "5:25-5:30"},
                    {"type": "recovery", "duration": "1:30"},
                ],
                [{"type": "warmup", "duration": "5:00", "heart_rate_max": 120}],
            ],
        }

        self.assertDictEqual(config, expected_config)


if __name__ == "__main__":
    unittest.main()
