import unittest

from garminworkouts.utils import functional


class FunctionalTestCase(unittest.TestCase):
    def test_filter_empty_value_is_none(self):
        value = {"k1": "v1", "k2": None}
        self.assertEqual(functional.filter_empty(value), {"k1": "v1"})

    def test_filter_empty_value_is_empty_array(self):
        value = {"k1": "v1", "k2": []}
        self.assertEqual(functional.filter_empty(value), {"k1": "v1"})

    def test_filter_empty_value_is_empty_dict(self):
        value = {"k1": "v1", "k2": {}}
        self.assertEqual(functional.filter_empty(value), {"k1": "v1"})

    def test_filter_empty_nested_value_is_none(self):
        value = {"k1": "v1", "k2": {"k3": "v3", "k4": None}}
        self.assertEqual(functional.filter_empty(value), {"k1": "v1", "k2": {"k3": "v3"}})

    def test_filter_empty_nested_value_is_empty_array(self):
        value = {"k1": "v1", "k2": {"k3": "v3", "k4": []}}
        self.assertEqual(functional.filter_empty(value), {"k1": "v1", "k2": {"k3": "v3"}})

    def test_filter_empty_nested_value_is_empty_dict(self):
        value = {"k1": "v1", "k2": {"k3": "v3", "k4": []}}
        self.assertEqual(functional.filter_empty(value), {"k1": "v1", "k2": {"k3": "v3"}})


if __name__ == "__main__":
    unittest.main()
