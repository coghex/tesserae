import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from tesserae.dice import DiceExpressionError, parse_expression, roll_expression


class DiceTests(unittest.TestCase):
    def test_signed_terms_and_arithmetic_are_auditable(self):
        draw = Mock(side_effect=[2, 5, 3])
        result = roll_expression(" 2d6 + 4 - d8 - 2 ", randint=draw)
        self.assertEqual(result.total, 6)
        self.assertEqual([t.subtotal for t in result.terms], [7, 4, -3, -2])
        self.assertEqual(result.as_dict()["terms"][2]["rolls"], [3])
        self.assertEqual(draw.call_args_list, [((1, 6),), ((1, 6),), ((1, 8),)])

    def test_natural_extremes_have_no_implicit_game_rules(self):
        draw = Mock(side_effect=[1, 10])
        result = roll_expression("2d10", randint=draw)
        self.assertEqual(result.total, 11)
        self.assertEqual(draw.call_count, 2)
        self.assertEqual(set(result.as_dict()), {"schema_version", "expression", "terms", "total"})

    def test_constants_and_one_sided_dice(self):
        draw = Mock(return_value=1)
        self.assertEqual(roll_expression("-4 + 2", randint=draw).total, -2)
        draw.assert_not_called()
        self.assertEqual(roll_expression("+D1", randint=draw).total, 1)

    def test_entire_expression_is_validated_before_randomness(self):
        invalid = ["", " ", "1d6 + nope", "1d6+", "2d6*3", "(1d6)", "1 2",
                   "0d6", "1d0", "2d-6", "--2d6", "1d6/0", "1d6; print(1)",
                   "1001d6", "600d6+401d6", "1d1000001", "1" * 4097]
        for expression in invalid:
            with self.subTest(expression=expression):
                draw = Mock()
                with self.assertRaises(DiceExpressionError):
                    roll_expression(expression, randint=draw)
                draw.assert_not_called()

    def test_resource_boundaries(self):
        self.assertEqual(parse_expression("1000d1000000")[0].count, 1000)

    def test_invalid_random_source_is_rejected(self):
        for value in [0, 7, 1.5, True]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    roll_expression("1d6", randint=Mock(return_value=value))


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "tesserae", *args],
                              cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True, check=False)

    def test_json_result_matches_individual_rolls(self):
        process = self.run_cli("roll", "2d6+3", "--json")
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        dice = result["terms"][0]["rolls"]
        self.assertEqual(len(dice), 2)
        self.assertTrue(all(1 <= value <= 6 for value in dice))
        self.assertEqual(result["total"], sum(dice) + 3)
        self.assertEqual(process.stderr, "")

    def test_plain_output_and_negative_expression(self):
        process = self.run_cli("roll", "--", "-2+1")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Total: -1", process.stdout)

    def test_invalid_expression_has_structured_error_and_no_result(self):
        process = self.run_cli("roll", "1d6+bad", "--json")
        self.assertEqual(process.returncode, 2)
        self.assertEqual(process.stdout, "")
        self.assertEqual(json.loads(process.stderr)["error"]["code"], "invalid_expression")

    def test_help(self):
        process = self.run_cli("--help")
        self.assertEqual(process.returncode, 0)
        self.assertIn("roll", process.stdout)


if __name__ == "__main__":
    unittest.main()
