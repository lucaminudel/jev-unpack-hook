[![Jev Compatible](https://img.shields.io/badge/Jev-v1.13+-blue)](https://typesafe.ai)
[![Supported Agents](https://img.shields.io/badge/Agents-Claude%20Code%20%7C%20Codex-green)](https://developers.openai.com/)

# Jev Unpack: a CodeX Stop hook that detects multi-topic multi-ask agent messages and paces conversations one ask at a time, adding relevant content and missing info

## The purpose of this Jev powered hook

As AI agents take on longer and more complex semi-autonomous tasks, they occasionally message the user out of the blue to ask for missing information, clarification, confirmation, feedback, choices, or approvals. These interruptions often take the form of a list containing multiple disparate issues and requests, lacking adequate context and clear explanations.

This suboptimal, unhuman interaction style increases cognitive fatigue and leads to more user errors. (*)

A **Skill** that instructs the agent to spot these multi-issue, multi-ask handovers and present them one at a time, with proper context and explanation, is quickly forgotten after a few turns. Conversely, using a **hook** to force the agent to check every message is excessively expensive and slow. Furthermore, an AI agent's ability to identify which messages actually require this kind of pacing is unreliable.

The [System One Jev model from TypeSafe AI](https://typesafe.ai/), on the other hand, accurately identifies which messages need step-by-step pacing, and invoking Jev on every message is both fast and cheap.

(*) [How to Reduce Cognitive Fatigue in AI-to-Human Handover](https://www.smharter.ai/blog/2026/10/09/how-to-reduce-cognitive-fatigue-in-ai-to-human-handover/)

## How this hook uses Jev

The hook receives the latest agent message from the `Stop` event.
Short messages of few lines are ignored.

For a longer message, the hook sends the message to the TypeSafe System One endpoint, using its most recent stable, official release AKA `jev-latest`. The request asks Jev two structured *Choive* questions:

1. How many Asks does the message contain: `Zero`, `One`, or `Two_or_more`?
2. How many Points does it contain: `Single_point`, `Two_or_three_points`, or `More_than_three_points`?

The hook validates both returned choices and their confidence values before making a routing decision:

- If Jev returns `Two_or_more` Asks and `More_than_three_points` with confidence of at least `0.80` for both answers, the hook blocks the normal stop and supplies the one-issue-at-a-time continuation instructions.
- If the same classification has confidence of at least `0.50` but below `0.80`, the hook blocks with a cautious confirmation prompt asking whether the user wants to go through the points one at a time.
- Otherwise, the hook returns `{"continue": true}` and allows the response to finish normally.

The hook also prevents a loop when the agent invokes the `Stop` hook again for its continuation by allowing events with `stop_hook_active` to continue immediately.

## CodeX hook installation instructions

Copy `src/jev_unpack/interaction_protocol_review_with_jev.py` to the Codex hooks directory.

For one project, copy it to:

```text
<project>/.codex/hooks/interaction_protocol_review_with_jev.py
```

For all projects for your user, copy it to:

```text
~/.codex/hooks/interaction_protocol_review_with_jev.py
```

The hook imports `python-dotenv`. Install that dependency for the Python interpreter that runs the hook:

```bash
python3 -m pip install --user python-dotenv
```


### Enable and trust the hook in CodeX GUI

For one project, copy the file `codex project hooks.json` in the project folder mentioned before, next to the hook file you have copied before.
For all user project, copy the file `codex global hooks.json` in the user's folder mentioned before, next to the hook file you have copied before.
Then rename the file to `hooks.json`.

1. Open Codex GUI.
2. Open Settings.
3. Open Hooks.
4. Find the copied hook.
5. Enable and trust it.

Review the displayed command and file path before trusting it. Codex may require trust again after the hook file changes.

### Enable and trust the hook in CodeX CLI

Start Codex from a terminal:

```bash
codex
```

Then run this command inside Codex:

```text
/hooks
```

Find the copied hook, review it, and enable or trust it. The CLI can also disable the hook from the same screen.

## Claude Code hook installation instructions

To be done.

## How to configure the hook

The hook has one required runtime configuration and several optional source-level settings.



### Required: TypeSafe API key

The hook reads the API key in this order:

1. the `TYPESAFE_API_KEY` environment variable;
2. the macOS Keychain generic password with service name `typesafe_api_key`.

To set the environment variable:

- copy the `.env.example` file next to the hook file you copied before, in the project foleder or in the user folder.
- edit the file and replace the text `your_real_api_key` with your API
- rename the file to `.env`

Do not commit `.env` or share it, keep your key private. 

Keychain is a macOS secure storage, that can be used instead of the .env file.
You can store your API key in Keychain under the "typesafe_api_key" application password.
And the hool will use those when it does not find the environment variable set.

You may want to add a fall-back to a different secure storage as implemente by your OS ok 
password manager tool.

### Required: TypeSafe API access url

By default the hook sends its review request to:

```text
https://api.typesafe.ai/v1/systemone
```

If your TypeSafe account uses a different System One endpoint, change that constant in the copied Python file only.

### Optional: diagnostics

Debug logging is disabled by default:

```python
DEBUG_LOGGING_ON = False
```

To diagnose the hook, temporarily set it to `True` in the copied file. The hook writes JSONL diagnostics to `.codex_stop_messages.jsonl` in its current working directory and does not log the API key. Turn logging off after diagnosis.

## How to run the hook tests

Run the tests from the repository root, with Python's standard-library test runner:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Just make sure that, as described in the installation instructions above, you have installed the `python-dotenv` dependency first.
