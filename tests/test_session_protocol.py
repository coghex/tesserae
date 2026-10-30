import json
import unittest

from tesserae.protocol import (
    MAX_REVISION,
    MAX_TOKEN_LENGTH,
    SESSION_ERROR_MESSAGES,
    CommandRequest,
    JoinRequest,
    ProtocolError,
    SyncRequest,
    encode_message,
    parse_client_message,
    parse_request,
    revision_message,
    session_error,
    snapshot_message,
)

TOKEN = "tsr1_9f8e7d6c5b4a39281706f5e4d3c2b1a0-SECRET"
GAME = "6f1c2a9e-3b7d-4c51-9a2e-8d0f4b6c1e37"


def join(**changes):
    data = {"version": 1, "type": "join", "token": TOKEN, **changes}
    return json.dumps({k: v for k, v in data.items() if v is not ...})


# (description, raw message, expected code)
JOIN_REJECTIONS = [
    ("malformed json", '{"version": 1, "type": "join", "token": "' + TOKEN, "invalid_request"),
    ("duplicate token", join()[:-1] + f', "token": "{TOKEN}"}}', "invalid_request"),
    ("missing token", join(token=...), "invalid_request"),
    ("extra field", join(name="Alice"), "invalid_request"),
    ("claimed participant", join(participant_id="alice"), "invalid_request"),
    ("token not string", join(token=12345), "invalid_request"),
    ("bool version", join(version=True), "invalid_request"),
    ("unsupported version", join(version=2), "unsupported_version"),
    ("empty token", join(token=""), "authentication_failed"),
    ("token with space", join(token=TOKEN + " x"), "authentication_failed"),
    ("token too long", join(token="t" * (MAX_TOKEN_LENGTH + 1)), "authentication_failed"),
]  # fmt: skip


class SessionMessageParsingTests(unittest.TestCase):
    def test_join_and_sync_parse_in_any_field_order(self):
        reordered = json.dumps({"token": TOKEN, "type": "join", "version": 1})
        self.assertEqual(parse_client_message(reordered), JoinRequest(TOKEN))
        self.assertEqual(parse_client_message(join().encode()), JoinRequest(TOKEN))
        sync = '{"type": "sync", "version": 1}'
        self.assertEqual(parse_client_message(sync), SyncRequest())

    def test_commands_parse_identically_through_either_entry_point(self):
        command = json.dumps(
            {
                "version": 1,
                "type": "command",
                "request_id": "r1",
                "expected_revision": 0,
                "command": "roll",
                "args": {"expression": "2d6+3"},
            }
        )
        parsed = parse_client_message(command)
        self.assertIsInstance(parsed, CommandRequest)
        self.assertEqual(parsed, parse_request(command))

    def test_join_rejections_never_reveal_the_token(self):
        for description, raw, code in JOIN_REJECTIONS:
            with self.subTest(description):
                with self.assertRaises(ProtocolError) as caught:
                    parse_client_message(raw)
                error = caught.exception
                self.assertEqual(error.code, code)
                self.assertIsNone(error.request_id)
                for shown in (
                    str(error),
                    repr(error),
                    encode_message(error.to_message()),
                ):
                    self.assertNotIn("SECRET", shown)
        self.assertNotIn("SECRET", repr(JoinRequest(TOKEN)))

    def test_bad_token_format_looks_like_any_failed_authentication(self):
        with self.assertRaises(ProtocolError) as caught:
            parse_client_message(join(token="has space"))
        self.assertEqual(
            caught.exception.to_message(), session_error("authentication_failed")
        )

    def test_sync_and_other_types_are_strict(self):
        cases = [
            ('{"version": 1, "type": "sync", "revision": 3}', "invalid_request"),
            (
                '{"version": 1, "type": "sync", "participant_id": "a"}',
                "invalid_request",
            ),
            ('{"version": 2, "type": "sync"}', "unsupported_version"),
            ('{"version": 1, "type": "chat", "text": "hi"}', "invalid_request"),
            ('{"version": 1}', "invalid_request"),
            ("[]", "invalid_request"),
        ]
        for raw, code in cases:
            with self.subTest(raw):
                with self.assertRaises(ProtocolError) as caught:
                    parse_client_message(raw)
                self.assertEqual(caught.exception.code, code)


class SessionMessageFormattingTests(unittest.TestCase):
    def assertRoundTrips(self, message, expected):
        self.assertEqual(message, expected)
        self.assertEqual(json.loads(encode_message(message)), expected)

    def test_snapshot_is_only_the_allowlisted_metadata(self):
        self.assertRoundTrips(
            snapshot_message(GAME, "p_7h3k9q2m", 4),
            {
                "version": 1,
                "type": "snapshot",
                "game_id": GAME,
                "participant_id": "p_7h3k9q2m",
                "revision": 4,
            },
        )

    def test_revision_notification_names_no_participant(self):
        self.assertRoundTrips(
            revision_message(GAME, 5),
            {"version": 1, "type": "revision", "game_id": GAME, "revision": 5},
        )

    def test_session_errors_have_fixed_messages_and_no_view(self):
        for code, message in SESSION_ERROR_MESSAGES.items():
            with self.subTest(code):
                self.assertRoundTrips(
                    session_error(code),
                    {
                        "version": 1,
                        "type": "error",
                        "request_id": None,
                        "revision": None,
                        "code": code,
                        "message": message,
                    },
                )

    def test_formatters_reject_values_outside_the_contract(self):
        for call in (
            lambda: snapshot_message("", "p1", 0),
            lambda: snapshot_message(GAME, "has space", 0),
            lambda: snapshot_message(GAME, "p1", -1),
            lambda: revision_message(GAME, MAX_REVISION + 1),
            lambda: revision_message(None, 1),
        ):
            with self.assertRaises(ValueError):
                call()
        with self.assertRaises(KeyError):
            session_error("invalid_request")
