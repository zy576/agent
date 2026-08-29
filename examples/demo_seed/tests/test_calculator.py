import unittest

from calculator import add_many, sum_pair


class CalculatorTests(unittest.TestCase):
    def test_sum_pair(self):
        self.assertEqual(sum_pair(2, 3), 5)

    def test_add_many_list(self):
        self.assertEqual(add_many([1, 2, 3]), 6)

    def test_add_many_empty_list(self):
        self.assertEqual(add_many([]), 0)


if __name__ == "__main__":
    unittest.main()

