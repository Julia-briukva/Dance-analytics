import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from parse_protocols import JudgeRef, parse_mark_string  # noqa: E402


def judges(count: int) -> list[JudgeRef]:
    return [JudgeRef(index + 1, chr(ord("A") + index), f"Judge {index + 1}", index + 1) for index in range(count)]


class ParseMarkStringTests(unittest.TestCase):
    def test_not_available_is_not_char_wise(self) -> None:
        self.assertEqual(parse_mark_string("#Н/Д", judges(4)), [])

    def test_aggregate_place_is_not_judge_mark(self) -> None:
        self.assertEqual(parse_mark_string("5,5", judges(3)), [])

    def test_numeric_places_when_length_matches_judges(self) -> None:
        parsed = parse_mark_string("235352", judges(6))

        self.assertEqual(
            [(judge.judge_index, mark) for judge, mark in parsed],
            [("A", "2"), ("B", "3"), ("C", "5"), ("D", "3"), ("E", "5"), ("F", "2")],
        )

    def test_crosses_skip_empty_positions(self) -> None:
        parsed = parse_mark_string("ABC-E", judges(5))

        self.assertEqual(
            [(judge.judge_index, mark) for judge, mark in parsed],
            [("A", "A"), ("B", "B"), ("C", "C"), ("E", "E")],
        )


if __name__ == "__main__":
    unittest.main()
