from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from email.message import Message
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

from jev_unpack import interaction_protocol_review_with_jev as hook


def choice_result(
    ask_choice: str = "Two_or_more",
    ask_confidence: float = 0.9,
    point_choice: str = "More_than_three_points",
    point_confidence: float = 0.9,
):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "ask_count": {
                "type": "choice",
                "choice": ask_choice,
                "confidence": ask_confidence,
                "probabilities": {},
            },
            "point_count": {
                "type": "choice",
                "choice": point_choice,
                "confidence": point_confidence,
                "probabilities": {},
            },
        },
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


class BytesResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None


def response_for(result):
    return BytesResponse(json.dumps(result).encode("utf-8"))


class HookTests(unittest.TestCase):
    def setUp(self):
        hook.DEBUG_LOGGING_ON = False

    def run_event(self, event):
        output = io.StringIO()
        with redirect_stdout(output):
            hook.handle_hook_event(event)
        return json.loads(output.getvalue())

    def test_short_message_does_not_read_keychain_or_call_jev(self):
        with patch.object(hook, "read_typesafe_api_key_from_keychain", side_effect=AssertionError("keychain called")), patch.object(hook, "system_one_request", side_effect=AssertionError("Jev called")):
            result = self.run_event({"last_assistant_message": "one\ntwo\nthree"})
        self.assertEqual(result, {"continue": True})

    def test_six_non_empty_logical_lines_trigger_jev(self):
        with patch.object(hook, "read_typesafe_api_key_from_keychain", return_value="key"), patch.object(hook, "system_one_request", return_value=choice_result()):
            result = self.run_event({"last_assistant_message": "one\n\ntwo\nthree\nfour\nfive\nsix"})
        self.assertEqual(result["decision"], "block")

    def test_960_characters_trigger_jev(self):
        with patch.object(hook, "read_typesafe_api_key_from_keychain", return_value="key"), patch.object(hook, "system_one_request", return_value=choice_result()):
            result = self.run_event({"last_assistant_message": "x" * 960})
        self.assertEqual(result["decision"], "block")

    def test_consecutive_newlines_do_not_count_as_extra_logical_lines(self):
        self.assertEqual(hook.count_non_empty_logical_lines("one\n\ntwo\n\nthree"), 3)
        self.assertFalse(hook.is_long_enough_to_trigger_a_review("one\n\ntwo\n\nthree\n\nfour\n\n\n"))

    def test_confident_routing_uses_automatic_prompt(self):
        with patch.object(hook, "read_typesafe_api_key_from_keychain", return_value="key"), patch.object(hook, "system_one_request", return_value=choice_result(ask_confidence=0.8, point_confidence=0.8)):
            result = self.run_event({"last_assistant_message": "\n".join(str(i) for i in range(6))})
        self.assertIn("The previous response contains multiple independently answerable issues", result["reason"])
        self.assertNotIn("Would you like to go through these points", result["reason"])

    def test_cautious_routing_uses_confirmation_prompt(self):
        with patch.object(hook, "read_typesafe_api_key_from_keychain", return_value="key"), patch.object(hook, "system_one_request", return_value=choice_result(ask_confidence=0.51, point_confidence=0.51)):
            result = self.run_event({"last_assistant_message": "\n".join(str(i) for i in range(6))})
        self.assertIn("Would you like to go through these points", result["reason"])
        self.assertIn("Go ahead", result["reason"])

    def test_rejected_routing_allows_continuation(self):
        result_data = choice_result(
            ask_choice="Two_or_more",
            ask_confidence=0.9,
            point_choice="Two_or_three_points",
            point_confidence=0.9,
        )
        with patch.object(hook, "read_typesafe_api_key_from_keychain", return_value="key"), patch.object(hook, "system_one_request", return_value=result_data):
            result = self.run_event({"last_assistant_message": "\n".join(str(i) for i in range(6))})
        self.assertEqual(result, {"continue": True})

    def test_stop_hook_active_skips_external_calls(self):
        with patch.object(hook, "read_typesafe_api_key_from_keychain", side_effect=AssertionError("keychain called")), patch.object(hook, "system_one_request", side_effect=AssertionError("Jev called")):
            result = self.run_event({"stop_hook_active": True, "last_assistant_message": "\n".join(str(i) for i in range(6))})
        self.assertEqual(result, {"continue": True})

    def test_missing_message_stops_with_warning(self):
        result = self.run_event({})
        self.assertFalse(result["continue"])
        self.assertIn("no assistant message", result["systemMessage"])

    def test_malformed_event_message_stops_with_warning(self):
        result = self.run_event({"last_assistant_message": ["not", "text"]})
        self.assertFalse(result["continue"])
        self.assertIn("unexpected format", result["systemMessage"])

    def test_invalid_stdin_json_stops_with_warning(self):
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("{")), redirect_stdout(output):
            hook.main()
        result = json.loads(output.getvalue())
        self.assertFalse(result["continue"])
        self.assertIn("invalid input", result["systemMessage"])

    def test_non_object_stdin_json_stops_with_warning(self):
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("[]")), redirect_stdout(output):
            hook.main()
        result = json.loads(output.getvalue())
        self.assertFalse(result["continue"])
        self.assertIn("unexpected event shape", result["systemMessage"])

    def test_validate_jev_result_accepts_documented_choice_response(self):
        result = choice_result()
        self.assertIs(hook.validate_jev_result(result), result)

    def test_validate_jev_result_rejects_invalid_shapes(self):
        invalid_results = [
            [],
            {},
            {"answers": {}},
            {"answers": {"ask_count": {"choice": "invalid", "confidence": 0.9}, "point_count": {"choice": "More_than_three_points", "confidence": 0.9}}},
            {"answers": {"ask_count": {"choice": "Two_or_more", "confidence": 1.1}, "point_count": {"choice": "More_than_three_points", "confidence": 0.9}}},
        ]
        for invalid_result in invalid_results:
            with self.subTest(invalid_result=invalid_result), self.assertRaises(
                (TypeError, ValueError)
            ):
                hook.validate_jev_result(invalid_result)

    def test_keychain_reads_trimmed_key(self):
        completed = SimpleNamespace(stdout="  secret-key\n")
        with patch.object(hook.subprocess, "run", return_value=completed) as run:
            self.assertEqual(hook.read_typesafe_api_key_from_keychain(), "secret-key")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"], hook.KEYCHAIN_COMMAND_TIMEOUT_SECONDS)

    def test_environment_key_is_preferred_and_trimmed(self):
        with patch.dict(
            hook.os.environ, {"TYPESAFE_API_KEY": "  environment-key  "}
        ), patch.object(
            hook,
            "read_typesafe_api_key_from_keychain",
            side_effect=AssertionError("keychain should not be called"),
        ):
            self.assertEqual(hook.read_typesafe_api_key(), "environment-key")

    def test_missing_environment_key_falls_back_to_keychain(self):
        with patch.dict(hook.os.environ, {}, clear=True), patch.object(
            hook,
            "read_typesafe_api_key_from_keychain",
            return_value="keychain-key",
        ) as read_keychain:
            self.assertEqual(hook.read_typesafe_api_key(), "keychain-key")
        read_keychain.assert_called_once()

    def test_empty_environment_key_falls_back_to_keychain(self):
        with patch.dict(hook.os.environ, {"TYPESAFE_API_KEY": "   "}), patch.object(
            hook,
            "read_typesafe_api_key_from_keychain",
            return_value="keychain-key",
        ) as read_keychain:
            self.assertEqual(hook.read_typesafe_api_key(), "keychain-key")
        read_keychain.assert_called_once()

    def test_keychain_retries_timeout_then_succeeds(self):
        completed = SimpleNamespace(stdout="secret-key")
        with patch.object(hook.subprocess, "run", side_effect=[subprocess.TimeoutExpired("security", 5), completed]) as run, patch.object(hook.time, "sleep") as sleep:
            self.assertEqual(hook.read_typesafe_api_key_from_keychain(), "secret-key")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once()

    def test_keychain_does_not_retry_missing_item(self):
        error = subprocess.CalledProcessError(44, "security", stderr="SecKeychainSearchCopyNext: The specified item could not be found.")
        with (
            patch.object(hook.subprocess, "run", side_effect=error) as run,
            patch.object(hook.time, "sleep") as sleep,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            hook.read_typesafe_api_key_from_keychain()
        run.assert_called_once()
        sleep.assert_not_called()

    def test_api_sends_bearer_request_and_parses_response(self):
        result = choice_result()
        with patch.object(hook, "urlopen", return_value=response_for(result)) as urlopen:
            actual = hook.system_one_request("message", "secret-key")
        self.assertEqual(actual, result)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertEqual(request.get_method(), "POST")

    def test_api_retries_network_error_then_succeeds(self):
        result = choice_result()
        with patch.object(hook, "urlopen", side_effect=[URLError("temporary DNS failure"), response_for(result)]) as urlopen, patch.object(hook.time, "sleep") as sleep:
            self.assertEqual(hook.system_one_request("message", "secret-key"), result)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_api_rejects_malformed_json_response(self):
        with patch.object(hook, "urlopen", return_value=BytesResponse(b"{")), self.assertRaises(ValueError):
            hook.system_one_request("message", "secret-key")

    def test_api_does_not_retry_authentication_failure(self):
        error = HTTPError(
            "https://api.typesafe.ai/v1/systemone",
            401,
            "Unauthorized",
            Message(),
            io.BytesIO(b"{}"),
        )
        with (
            patch.object(hook, "urlopen", side_effect=error) as urlopen,
            patch.object(hook.time, "sleep") as sleep,
            self.assertRaises(HTTPError),
        ):
            hook.system_one_request("message", "secret-key")
        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_retry_after_delay_is_bounded_by_retry_loop(self):
        headers = Message()
        headers["Retry-After"] = "999"
        error = HTTPError(
            "https://api.typesafe.ai/v1/systemone",
            429,
            "Too Many Requests",
            headers,
            io.BytesIO(),
        )

        def always_fail():
            raise error

        with patch.object(hook.time, "sleep") as sleep, self.assertRaises(HTTPError):
            hook.call_with_retries(always_fail, hook.is_retryable_api_error, hook.api_retry_delay)
        self.assertEqual(sleep.call_args_list, [call(hook.MAX_RETRY_AFTER_DELAY_SECONDS)] * 2)


if __name__ == "__main__":
    unittest.main()
