import json
import unittest
from unittest.mock import Mock, patch

from tesserae.dice import roll_expression
from tesserae.protocol import (
    MAX_REVISION,
    ProtocolError,
    encode_message,
    error_message,
    parse_client_message,
    parse_request,
    result_message,
    welcome_message,
)

VALID = {
    "version": 1,
    "type": "command",
    "request_id": "r1",
    "expected_revision": 0,
    "command": "roll",
    "args": {"expression": "2d6+3"},
}


def request(**changes):
    data = {**VALID, **changes}
    return json.dumps({k: v for k, v in data.items() if v is not ...})


# (description, raw request, expected code, expected recovered request_id)
REJECTIONS = [
    ("malformed json", '{"version": 1,', "invalid_request", None),
    ("not an object", "[1, 2]", "invalid_request", None),
    ("null", "null", "invalid_request", None),
    ("deep nesting", '{"request_id": "r1", "args": ' + "[" * 10**6 + "]" * 10**6 + "}", "invalid_request", None),
    ("duplicate key", request()[:-1] + ', "command": "roll"}', "invalid_request", None),
    ("duplicate arg key", request(args=...)[:-1] + ', "args": {"expression": "1", "expression": "2"}}', "invalid_request", None),
    ("NaN", request(expected_revision=...)[:-1] + ', "expected_revision": NaN}', "invalid_request", None),
    ("Infinity", request(expected_revision=...)[:-1] + ', "expected_revision": -Infinity}', "invalid_request", None),
    ("float overflow", request(expected_revision=...)[:-1] + ', "expected_revision": 1e999}', "invalid_request", None),
    ("missing field", request(args=...), "invalid_request", "r1"),
    ("extra field", request(extra=1), "invalid_request", "r1"),
    ("bool version", request(version=True), "invalid_request", "r1"),
    ("string version", request(version="1"), "invalid_request", "r1"),
    ("wrong type", request(type="result"), "invalid_request", "r1"),
    ("empty request_id", request(request_id=""), "invalid_request", None),
    ("long request_id", request(request_id="x" * 129), "invalid_request", None),
    ("spaced request_id", request(request_id=" r1"), "invalid_request", None),
    ("inner whitespace request_id", request(request_id="r\t1"), "invalid_request", None),
    ("numeric request_id", request(request_id=1), "invalid_request", None),
    ("bool revision", request(expected_revision=False), "invalid_request", "r1"),
    ("float revision", request(expected_revision=0.0), "invalid_request", "r1"),
    ("negative revision", request(expected_revision=-1), "invalid_request", "r1"),
    ("revision too large", request(expected_revision=MAX_REVISION + 1), "invalid_request", "r1"),
    ("command not string", request(command=None), "invalid_request", "r1"),
    ("args not object", request(args=["2d6"]), "invalid_request", "r1"),
    ("roll args extra", request(args={"expression": "d6", "x": 1}), "invalid_request", "r1"),
    ("roll args missing", request(args={}), "invalid_request", "r1"),
    ("expression not string", request(args={"expression": 6}), "invalid_request", "r1"),
    ("structure before version", request(version=2, expected_revision=-1), "invalid_request", "r1"),
    ("structure before command", request(command="attack", type="x"), "invalid_request", "r1"),
    ("unsupported version", request(version=2), "unsupported_version", "r1"),
    ("version before command", request(version=0, command="attack"), "unsupported_version", "r1"),
    ("version before expression", request(version=2, args={"expression": "x"}), "unsupported_version", "r1"),
    ("unsupported command", request(command="attack", args={"target": "orc"}), "unsupported_command", "r1"),
    ("command before expression", request(command="Roll"), "unsupported_command", "r1"),
    ("invalid expression", request(args={"expression": "2d6*3"}), "invalid_expression", "r1"),
    ("dice limit", request(args={"expression": "1001d6"}), "invalid_expression", "r1"),
]  # fmt: skip


class ProtocolValidationTests(unittest.TestCase):
    def test_valid_request_preserves_values(self):
        raw = json.dumps(
            {
                "args": {"expression": " 2d6 + 3 "},
                "command": "roll",
                "expected_revision": MAX_REVISION,
                "request_id": "é-" + "x" * 126,
                "type": "command",
                "version": 1,
            }
        )
        parsed = parse_request(raw)
        self.assertEqual(parsed.request_id, "é-" + "x" * 126)
        self.assertEqual(parsed.expected_revision, MAX_REVISION)
        self.assertEqual(parsed.args, {"expression": " 2d6 + 3 "})
        self.assertEqual(parse_request(raw.encode()), parsed)

    def test_rejections_have_stable_codes(self):
        # Session parsing must not change how any command is judged.
        for parse in (parse_request, parse_client_message):
            for description, raw, code, request_id in REJECTIONS:
                with self.subTest(parse=parse.__name__, case=description):
                    with self.assertRaises(ProtocolError) as caught:
                        parse(raw)
                    self.assertEqual(caught.exception.code, code)
                    self.assertEqual(caught.exception.request_id, request_id)
                    self.assertTrue(caught.exception.message)

    def test_rejected_request_formats_as_error(self):
        with self.assertRaises(ProtocolError) as caught:
            parse_request(request(version=2))
        message = caught.exception.to_message(revision=4)
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["code"], "unsupported_version")
        self.assertEqual(message["request_id"], "r1")
        self.assertEqual(message["revision"], 4)

    def test_replay_content_ignores_revision_and_key_order(self):
        first = parse_request(request(expected_revision=0))
        retry = json.loads(request(expected_revision=7, request_id="r2"))
        retry = parse_request(json.dumps(dict(reversed(retry.items()))))
        self.assertEqual(first.content(), retry.content())
        spaced = parse_request(request(args={"expression": "2d6 + 3"}))
        self.assertNotEqual(first.content(), spaced.content())
        first.content()["args"]["expression"] = "1d4"
        self.assertEqual(first.content(), retry.content())

    def test_validation_and_formatting_need_no_randomness_database_or_socket(self):
        forbidden = AssertionError("not allowed during validation")
        with (
            patch("tesserae.dice.SystemRandom") as system_random,
            patch("sqlite3.connect", side_effect=forbidden),
            patch("socket.socket", side_effect=forbidden),
        ):
            parse_request(request())
            for _, raw, _, _ in REJECTIONS:
                with self.assertRaises(ProtocolError):
                    parse_request(raw)
            for code in ("stale_revision", "request_conflict", "internal_error"):
                encode_message(error_message(code, "x", request_id="r1", revision=3))
            encode_message(welcome_message(0))
        system_random.assert_not_called()


class ProtocolMessageTests(unittest.TestCase):
    def assertRoundTrips(self, message, expected):
        self.assertEqual(message, expected)
        self.assertEqual(json.loads(encode_message(message)), expected)

    def test_result_wraps_dice_result_unchanged(self):
        result = roll_expression("2d6-1", randint=Mock(side_effect=[2, 5]))
        self.assertRoundTrips(
            result_message("r1", 1, result),
            {
                "version": 1,
                "type": "result",
                "request_id": "r1",
                "revision": 1,
                "result": {
                    "schema_version": 1,
                    "expression": "2d6-1",
                    "terms": [
                        {"kind": "dice", "count": 2, "sides": 6, "rolls": [2, 5],
                         "sign": 1, "subtotal": 7},
                        {"kind": "constant", "value": 1, "sign": -1, "subtotal": -1},
                    ],
                    "total": 6,
                },
            },
        )  # fmt: skip

    def test_error_and_welcome_match_contract(self):
        self.assertRoundTrips(
            error_message("invalid_request", "Bad."),
            {
                "version": 1,
                "type": "error",
                "request_id": None,
                "revision": None,
                "code": "invalid_request",
                "message": "Bad.",
            },
        )
        self.assertRoundTrips(
            welcome_message(0), {"version": 1, "type": "welcome", "revision": 0}
        )

    def test_formatters_reject_values_outside_the_contract(self):
        result = roll_expression("1", randint=Mock())
        for call in (
            lambda: welcome_message(-1),
            lambda: welcome_message(True),
            lambda: welcome_message(MAX_REVISION + 1),
            lambda: result_message("r 1", 1, result),
            lambda: error_message("not_a_code", "x"),
            lambda: error_message("internal_error", "x", revision=1.0),
        ):
            with self.assertRaises(ValueError):
                call()
