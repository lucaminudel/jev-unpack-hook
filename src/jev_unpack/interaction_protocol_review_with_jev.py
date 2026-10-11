"""Interaction Protocol Review with Jev: identify and respond to multiple complex requests 
   with a prompt that breaks down the complex message into single question messages

The Codex / Claude Code Stop hook receives the latest assistant message. It allows
short messages to finish without an external call. For longer messages it reads a
TypeSafe API key, asks Jev to classify the number of Asks and Points, and 
returns the editable continuation prompt only when the classification matches 
the interaction-protocol rule.

Set DEBUG_LOGGING_ON to True while diagnosing the hook. When enabled, the hook
appends input messages, skip decisions, Jev responses, and error details to the
local JSONL log. It never logs the API key.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from typing import TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOOK_VERSION = "1.0.0"

# Enable temporarily when diagnosing this hook behavior.
# DEBUG_LOGGING_ON = True
DEBUG_LOGGING_ON = False
DEBUG_LOGGING_PATH = Path.cwd() / ".jev-unpack-hook_stop_messages_logs.jsonl"

# ################################################################# #
# ################################################################# #
#                                                                   #
#              CONFIGURE THIS BEFORE USING THIS HOOK                #
#                                                                   #
# ################################################################# #
# ################################################################# #
#
#
# Configure the TypeSafe's Jev API url
# based on where you have the account.
# After this, look below to set where to read your TypeSafe API key.
TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"


# Constants used to calculate when a message is too short to be considered
MINIMUM_MESSAGE_LINES_TO_TRIGGER_A_REVIEW = 6
SCREEN_WIDTH_CHARACTERS = 160
MINIMUM_MESSAGE_CHARACTERS_TO_TRIGGER_A_REVIEW = (
    SCREEN_WIDTH_CHARACTERS * MINIMUM_MESSAGE_LINES_TO_TRIGGER_A_REVIEW
)

# Thresholds for the Jev Confidence-gated routing pattern implemented below
AUTO_CONTINUE_MIN_CONFIDENCE = 0.80
CAUTIOUS_CONFIRMATION_MIN_CONFIDENCE = 0.50

# Retry configuration for all the external calls
# The number of retry attempts and delay between retries adds up to less then 60 sec
# Keep the command hook timeout in .codex/hooks.json and .claude/settings.json at or above 90 seconds.
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (0.5, 1.0)
KEYCHAIN_COMMAND_TIMEOUT_SECONDS = 5
API_REQUEST_TIMEOUT_SECONDS = 5
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504, 529}
MAX_RETRY_AFTER_DELAY_SECONDS = 10.0
RetryResult = TypeVar("RetryResult")




# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#                                             #
#  TAILOR THE RESPONSE PROMPT IF YOU WANT TO  #
#                                             #
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#
ONE_ISSUE_AT_A_TIME_INSTRUCTIONS = """\

Choose the first unresolved issue and start with that one.

Your response about the selected issue must contain:

1. Brief context:
   - the relevant business outcome or impact (if more than one has been discussed in this session);
   - the affected user, stakeholder, and their related goal (if more than one has been discussed in this session);
   - the affected part of the feature;
   - the affected source code module, namespace, or file;
   - the practical consequence of the issue.

2. One clear explanation of the issue. You may include a graphical representation such as a flow chart, a screen snapshot,
   a table, a graph, or any other appropriate representation.

3. One ask for the user:
   - ask for only one answer, choice, confirmation, review, approval, or other response;
   - explain the available options when a choice is required;
   - explain the practical consequences of those options.

Keep the issue and the ask for the user clearly and unmistakable visually and graphically separated, better if you can use a colour, emoji, and/or icon.

Do not ask about any remaining issues until the user has provided their response for the current issue.

After the user has responded to this follow-up message, continue with the remaining unresolved issues,
presenting them one at a time and waiting for the user's response after each issue.
"""

# Edit this message to change the automatic continuation prompt sent back to Codex / Claude Code after a handover.
CONTINUATION_PROMPT_FOR_MULTI_REQUEST_MULTI_ISSUE_AGENT_MESSAGES = """\
The previous response contains multiple independently answerable issues and asks.
Do not present all of them together again.

""" + ONE_ISSUE_AT_A_TIME_INSTRUCTIONS

# Edit this message to change the confirmation prompt used when Jev is less certain.
CAUTIOUS_CONTINUATION_PROMPT_FOR_MULTI_REQUEST_MULTI_ISSUE_AGENT_MESSAGES = """\
The previous response may contain multiple independently answerable issues and asks.

Ask the user this single question:

Would you like to go through these points one at a time?

Wait for the user's answer. Do not answer the confirmation question yourself.

If the user answers Yes or gives an equivalent confirmation such as
"Go ahead" or "Do that", follow these instructions:

Do not present all of the independently answerable issues and asks together again.
""" + ONE_ISSUE_AT_A_TIME_INSTRUCTIONS  + """

If the user answers No, do not apply the one-issue-at-a-time protocol. Continue according to the user's response
and the normal conversation.
"""





def read_typesafe_api_key() -> str:



    # ################################################################# #
    # ################################################################# #
    #                                                                   #
    #              CONFIGURE THIS BEFORE USING THIS HOOK                #
    #                                                                   #
    # ################################################################# #
    # ################################################################# #
    #
    #
    # TypeSafe API key: 
    #     Set the environment variable TYPESAFE_API_KEY with your API key and makes it available here
    #     Or in macOS store your API key in Keychain under the "typesafe_api_key" application password
    #     You can also implement the access to the API key from an alternative secure storage available
    #     from your OS or your machine.
    #
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_path)
    
    environment_api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if environment_api_key:
        return environment_api_key
    return read_typesafe_api_key_from_keychain()





def allow_continuation() -> None:
    print(json.dumps({"continue": True}))


def emit_warning_and_stop(message: str, details: dict | None = None) -> None:
    if details is not None:
        try:
            append_log({"hook_warning": details})
        except (OSError, TypeError, ValueError):
            pass
        error_type = details.get("type")
        error_message = details.get("message")
        if error_type or error_message:
            message = f"{message} ({error_type or 'UnknownError'}: {error_message or 'No details'})"
    print(json.dumps({"continue": False, "systemMessage": message}))


def append_log(record: dict) -> None:
    if not DEBUG_LOGGING_ON:
        return
    with DEBUG_LOGGING_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_message(event: dict) -> None:
    message = event.get("last_assistant_message")
    if not isinstance(message, str):
        return

    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "session_id": event.get("session_id"),
        "turn_id": event.get("turn_id"),
    }
    append_log(record)


def call_with_retries(
    operation: Callable[[], RetryResult],
    should_retry: Callable[[Exception], bool],
    retry_delay: Callable[[Exception, int], float],
) -> RetryResult:
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            return operation()
        except Exception as error:
            if attempt == MAX_RETRY_ATTEMPTS - 1 or not should_retry(error):
                raise
            time.sleep(max(0.0, min(retry_delay(error, attempt), MAX_RETRY_AFTER_DELAY_SECONDS)))

    raise RuntimeError("The retried operation did not complete")


def retry_delay_seconds(attempt: int) -> float:
    return RETRY_DELAYS_SECONDS[min(attempt, len(RETRY_DELAYS_SECONDS) - 1)]


def is_transient_keychain_error(error: Exception) -> bool:
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    error_text = (error.stderr or "").lower()
    return any(
        phrase in error_text
        for phrase in (
            "temporarily unavailable",
            "service unavailable",
            "try again",
            "operation in progress",
        )
    )


def read_typesafe_api_key_from_keychain() -> str:
    def read_keychain_value() -> str:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "typesafe_api_key", "-w"],
            check=True,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_COMMAND_TIMEOUT_SECONDS,
        )
        api_key = result.stdout.strip()
        if not api_key:
            raise ValueError("The Keychain returned an empty TypeSafe API key")
        return api_key

    return call_with_retries(
        read_keychain_value,
        is_transient_keychain_error,
        lambda _error, attempt: retry_delay_seconds(attempt),
    )


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#                                              #
#  TAILOR THE Jev JSON PAYLOAD IF YOU WANT TO  #
#                                              #
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#
def build_system_one_payload(message: str) -> dict:
    return {
        "state": {
            "chat_message_content": message,
            "Ask": (
                "In a chat message, typically an agent handover message to a human, "
                "one Ask is an explicit or implied request that requires one response "
                "from the human. It may be one question to be answered with one Answer, "
                "one choice to be made, one request to review, confirm, comment, accept, "
                "or authorise, or one request for feedback, approval, or another response. "
                "A chat message may contain zero, one, or multiple Asks. One Ask may refer "
                "to one, some, or all Points. When an Ask requests or requires multiple "
                "Answers, for example, one for each Point, that such Ask counts as multiple "
                "Asks as many as the number of Answers it requires. An Ask is not a Point."
            ),
            "Answer": "One answer is one reply to one Ask.",
            "Point": (
                "A Point is a separately identifiable item presented to a human as part of "
                "a list. The list item may be marked with a bullet point, a number, "
                "punctuation, line breaks, or another clear separator. A Point may contain "
                "multiple related claims or facts, which may require human evaluation or "
                "serve as supporting context. If claims or facts are nested items of a list, "
                "each nested item counts as a separate Point; therefore, count nested list "
                "items as separate Points."
            ),
        },
        "model": "jev-latest",
        "questions": {
            "ask_count": {
                "type": "choice",
                "instructions": "How many Ask does the chat message contain?",
                "criteria": {
                    "Zero": {
                        "definition": "The chat message contains zero Ask.",
                        "test": "The human is not expected to reply to the message with an Answer.",
                    },
                    "One": {
                        "definition": "The chat message contains one Ask.",
                        "test": "The human is expected to reply to one Point or to reply to all Points with one Answer.",
                    },
                    "Two_or_more": {
                        "definition": "The chat message contains two or more Asks.",
                        "test": "The human is expected to reply to different Asks related to different Points with multiple Answers.",
                    },
                },
            },
            "point_count": {
                "type": "choice",
                "instructions": "How many Point does the chat message contain?",
                "criteria": {
                    "Single_point": "The chat message contains zero or one Point.",
                    "Two_or_three_points": "The chat message contains two or three Points.",
                    "More_than_three_points": "The chat message contains four or more Points.",
                },
            },
        },
    }




def is_retryable_api_error(error: Exception) -> bool:
    if isinstance(error, HTTPError):
        return error.code in RETRYABLE_HTTP_STATUS_CODES
    return isinstance(error, URLError)


def api_retry_delay(error: Exception, attempt: int) -> float:
    if isinstance(error, HTTPError):
        retry_after = error.headers.get("Retry-After")
        try:
            return float(retry_after) if retry_after else retry_delay_seconds(attempt)
        except ValueError:
            pass
    return retry_delay_seconds(attempt)


def system_one_request(message: str, api_key: str) -> dict:
    payload = build_system_one_payload(message)
    request = Request(
        TYPESAFE_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def send_request() -> dict:
        with urlopen(request, timeout=API_REQUEST_TIMEOUT_SECONDS) as response:
            return validate_jev_result(json.load(response))

    return call_with_retries(send_request, is_retryable_api_error, api_retry_delay)


def validate_jev_result(result: object) -> dict:
    if not isinstance(result, dict):
        raise TypeError("Jev response must be a JSON object")

    answers = result.get("answers")
    if not isinstance(answers, dict):
        raise TypeError("Jev response must contain an answers object")

    expected_choices = {
        "ask_count": {"Zero", "One", "Two_or_more"},
        "point_count": {"Single_point", "Two_or_three_points", "More_than_three_points"},
    }
    for question_name, valid_choices in expected_choices.items():
        answer = answers.get(question_name)
        if not isinstance(answer, dict):
            raise TypeError(f"Jev response is missing the {question_name} answer")

        choice = answer.get("choice")
        if choice not in valid_choices:
            raise ValueError(f"Jev response contains an invalid {question_name} choice")

        confidence = answer.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
        ):
            raise ValueError(f"Jev response contains an invalid {question_name} confidence")

    return result


def answer_choice(result: dict, question_name: str) -> str | None:
    answer = result.get("answers", {}).get(question_name, {})
    choice = answer.get("choice")
    return choice if isinstance(choice, str) else None


def answer_confidence(result: dict, question_name: str) -> float | None:
    answer = result.get("answers", {}).get(question_name, {})
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    return confidence if 0.0 <= confidence <= 1.0 else None


def count_non_empty_logical_lines(message: str) -> int:
    return sum(1 for line in re.split(r"\r?\n+", message) if line.strip())


def is_long_enough_to_trigger_a_review(message: str) -> bool:
    return (
        count_non_empty_logical_lines(message) >= MINIMUM_MESSAGE_LINES_TO_TRIGGER_A_REVIEW
        or len(message) >= MINIMUM_MESSAGE_CHARACTERS_TO_TRIGGER_A_REVIEW
    )


def is_multi_request_multi_issue(result: dict) -> bool:
    ask_count = answer_choice(result, "ask_count")
    point_count = answer_choice(result, "point_count")
    return ask_count == "Two_or_more" and point_count == "More_than_three_points"


def meets_confidence_threshold(result: dict, threshold: float) -> bool:
    ask_confidence = answer_confidence(result, "ask_count")
    point_confidence = answer_confidence(result, "point_count")
    return (
        ask_confidence is not None
        and point_confidence is not None
        and ask_confidence >= threshold
        and point_confidence >= threshold
    )


def classify_handover_confidently(result: dict) -> bool:
    return is_multi_request_multi_issue(result) and meets_confidence_threshold(
        result, AUTO_CONTINUE_MIN_CONFIDENCE
    )


def classify_handover_cautiously(result: dict) -> bool:
    return (
        is_multi_request_multi_issue(result)
        and not classify_handover_confidently(result)
        and meets_confidence_threshold(result, CAUTIOUS_CONFIRMATION_MIN_CONFIDENCE)
    )


def log_confidence_routing_decision(decision: str) -> None:
    try:
        append_log({"confidence_gated_routing_decision": decision})
    except (OSError, TypeError, ValueError):
        pass


def handle_hook_event(event: dict) -> None:
    # The continuation (response) caused by this hook triggers Stop again (returns here). 
    # Let that second Stop event finish so the static prompt cannot create an infinite loop.
    if event.get("stop_hook_active"):
        allow_continuation()
        return

    try:
        record_message(event)

        message = event.get("last_assistant_message")

        if message is None:
            emit_warning_and_stop(
                "The Interaction Protocol Review hook received no assistant message to inspect.",
                {"type": "MissingAssistantMessage"},
            )
            return

        if not isinstance(message, str):
            emit_warning_and_stop(
                "The Interaction Protocol Review hook received an assistant message in an unexpected format.",
                {"type": "MalformedAssistantMessage", "actual_type": type(message).__name__},
            )
            return

        if not message.strip():
            emit_warning_and_stop(
                "The Interaction Protocol Review hook received an empty assistant message to inspect.",
                {"type": "EmptyAssistantMessage"},
            )
            return


        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
        #                                                      #
        #  TAILOR THE MESSAGE-TOO-SHORT FILTER IF YOU WANT TO  #
        #                                                      #
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
        #
        if not is_long_enough_to_trigger_a_review(message):
            append_log({"jev_skipped": "message is too short to trigger the interaction protocol review."})
            allow_continuation()
            return

        api_key = read_typesafe_api_key()

        jev_result = system_one_request(message, api_key)
        append_log({"jev_result": jev_result})

    except HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        emit_warning_and_stop(
            "The Interaction Protocol Review hook could not contact Jev, the review has not happened.",
            {"type": type(error).__name__, "message": str(error), "body": error_body},
        )
        return
    except (OSError, TypeError, ValueError, subprocess.SubprocessError, URLError) as error:
        emit_warning_and_stop(
            "The Interaction Protocol Review hook encountered an environment or Jev error, the review has not happened.",
            {"type": type(error).__name__, "message": str(error)},
        )
        return

    try:


        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
        #                                               #
        #  TAILOR THE Jev CONFIDENCE PATTERN            #
        #  AND THE CONNECTED HOOK LOGIC IF YOU WANT TO  #
        #                                               #
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
        #

        # Jev Confidence-gated routing pattern implementation 
        # See: https://docs.typesafe.ai/patterns/confidence-routing

        if classify_handover_confidently(jev_result):
            log_confidence_routing_decision("confidently")
            print(json.dumps({"decision": "block", "reason": CONTINUATION_PROMPT_FOR_MULTI_REQUEST_MULTI_ISSUE_AGENT_MESSAGES}))
        elif classify_handover_cautiously(jev_result):
            log_confidence_routing_decision("cautiously") 
            print(json.dumps({"decision": "block", "reason": CAUTIOUS_CONTINUATION_PROMPT_FOR_MULTI_REQUEST_MULTI_ISSUE_AGENT_MESSAGES}))
        else:
            log_confidence_routing_decision("rejected")
            allow_continuation()



    except (AttributeError, KeyError, TypeError, ValueError) as error:
        emit_warning_and_stop(
            "The Interaction Protocol Review hook received an unexpected Jev response, the review has not happened.",
            {"type": type(error).__name__, "message": str(error)},
        )


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        emit_warning_and_stop(
            "The Interaction Protocol Review hook received invalid input and could not inspect the handover.",
            {"type": type(error).__name__, "message": str(error)},
        )
        return

    if not isinstance(event, dict):
        emit_warning_and_stop(
            "The Interaction Protocol Review hook received an unexpected event shape and could not inspect the handover.",
            {"type": "UnexpectedEventShape", "actual_type": type(event).__name__},
        )
        return

    handle_hook_event(event)


if __name__ == "__main__":
    main()
