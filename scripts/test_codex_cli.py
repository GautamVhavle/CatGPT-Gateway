#!/usr/bin/env python3
"""
Codex CLI compatibility test suite for POST /v1/responses.

Simulates real Codex CLI usage patterns:
  1. Simple coding question (streaming)
  2. Tool-calling: model requests shell command execution
  3. Multi-turn tool loop: question → function_call → function_call_output → answer
  4. Tool-calling: model requests apply_patch (file edit)
  5. Streaming with tool calls — SSE event validation
  6. Multiple tools defined simultaneously
  7. tool_choice="none" — tools defined but force text response
  8. function_call_output with error output

Codex CLI sends:
  - POST /v1/responses with stream=true
  - instructions (system prompt with coding agent personality)
  - tools in flat format: [{"type":"function","name":"shell","parameters":{...}}]
  - Multi-turn input with function_call and function_call_output items
  - tool_choice="auto" (default)

Prerequisites:
  - CatGPT API server running: python -m src.api.server
  - API_TOKEN must match .env (default: dummy123)

Usage:
  python scripts/test_codex_cli.py
  python scripts/test_codex_cli.py --test 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# ── Configuration ───────────────────────────────────────────────

DEFAULT_BASE = "http://localhost:8000"
API_KEY = os.environ.get("API_TOKEN", "dummy123")

PASSED = 0
FAILED = 0
SKIPPED = 0
ERRORS: list[str] = []

# Codex CLI system prompt (simplified version)
CODEX_SYSTEM_PROMPT = (
    "You are a coding assistant running in the user's terminal. "
    "You can execute shell commands and edit files using the provided tools. "
    "When the user asks you to do something, use the appropriate tool. "
    "Always respond concisely."
)

# Codex CLI tool definitions (flat Responses API format)
SHELL_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Execute a shell command on the user's machine. Returns stdout, stderr, and exit code.",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command and arguments to execute",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}

APPLY_PATCH_TOOL = {
    "type": "function",
    "name": "apply_patch",
    "description": "Apply a unified diff patch to edit files. Use this to create or modify files.",
    "parameters": {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "A unified diff patch string",
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
}


# ── Helpers ─────────────────────────────────────────────────────

def separator(title: str) -> None:
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print(f"{'=' * 64}")


def ok(msg: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  ✅ {msg}")


def fail(msg: str) -> None:
    global FAILED
    FAILED += 1
    ERRORS.append(msg)
    print(f"  ❌ {msg}")


def assert_eq(label, actual, expected):
    if actual == expected:
        ok(f"{label}: {actual!r}")
    else:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def assert_in(label, value, container):
    if value in container:
        ok(f"{label}: {value!r} found")
    else:
        fail(f"{label}: {value!r} not in {container!r}")


def assert_type(label, value, expected_type):
    if isinstance(value, expected_type):
        ok(f"{label}: type is {type(value).__name__}")
    else:
        fail(f"{label}: expected {expected_type.__name__}, got {type(value).__name__}")


def assert_truthy(label, value):
    if value:
        ok(f"{label}")
    else:
        fail(f"{label}: value is falsy: {value!r}")


def assert_nonempty_string(label, value):
    if isinstance(value, str) and len(value) > 0:
        ok(f"{label}: \"{value[:60]}{'...' if len(value) > 60 else ''}\"")
    else:
        fail(f"{label}: expected non-empty string, got {value!r}")


def parse_sse_events(text: str) -> list[dict]:
    events = []
    current_event = None
    current_data_lines = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            current_data_lines.append(line[6:])
        elif line == "" and current_event is not None:
            data_str = "\n".join(current_data_lines)
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                data = data_str
            events.append({"event": current_event, "data": data})
            current_event = None
            current_data_lines = []
    if current_event is not None and current_data_lines:
        data_str = "\n".join(current_data_lines)
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            data = data_str
        events.append({"event": current_event, "data": data})
    return events


def post_responses(client: httpx.Client, body: dict, stream: bool = False):
    if stream:
        return client.post("/v1/responses", json=body, timeout=300.0)
    r = client.post("/v1/responses", json=body, timeout=300.0)
    r.raise_for_status()
    return r.json()


# ── Tests ───────────────────────────────────────────────────────

def test_1_codex_simple_question(client: httpx.Client):
    """Codex CLI: simple coding question via streaming (no tools needed)."""
    separator("Test 1: Codex CLI — streaming coding question")

    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "catgpt-browser",
            "instructions": CODEX_SYSTEM_PROMPT,
            "input": "What does the 'ls' command do in Unix? Reply in one sentence.",
            "tools": [SHELL_TOOL, APPLY_PATCH_TOOL],
            "tool_choice": "auto",
            "stream": True,
        },
        timeout=300.0,
    ) as r:
        r.raise_for_status()
        body = r.read().decode()

    events = parse_sse_events(body)
    event_types = [e["event"] for e in events]

    assert_truthy("received SSE events", len(events) > 0)
    assert_in("has response.created", "response.created", event_types)
    assert_in("has response.completed", "response.completed", event_types)

    # Should be a text response (not a tool call) since this is a knowledge question
    completed = next((e["data"] for e in events if e["event"] == "response.completed"), {})
    resp = completed.get("response", {})
    assert_eq("status", resp.get("status"), "completed")
    assert_truthy("output present", len(resp.get("output", [])) > 0)

    output_text = resp.get("output_text", "")
    assert_nonempty_string("output_text", output_text)

    # Should have tools echoed even if not used
    assert_truthy("tools echoed", len(resp.get("tools", [])) >= 2)

    print(f"\n  Model replied: \"{output_text[:80]}\"")


def test_2_codex_tool_call_shell(client: httpx.Client):
    """Codex CLI: model should call the shell tool for a system task."""
    separator("Test 2: Codex CLI — shell tool call")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": "List the files in the current directory.",
        "tools": [SHELL_TOOL],
        "tool_choice": "auto",
        "stream": False,
    })

    assert_truthy("id starts with resp_", resp.get("id", "").startswith("resp_"))
    assert_eq("status", resp.get("status"), "completed")

    output = resp.get("output", [])
    assert_truthy("output has items", len(output) > 0)

    first = output[0]
    output_type = first.get("type")

    if output_type == "function_call":
        assert_eq("function_call.name", first.get("name"), "shell")
        assert_truthy("function_call.call_id present", first.get("call_id"))
        assert_eq("function_call.status", first.get("status"), "completed")

        # Parse arguments
        args = json.loads(first.get("arguments", "{}"))
        assert_truthy("arguments.command present", "command" in args)
        assert_type("arguments.command is list", args["command"], list)

        print(f"\n  Model called: shell({args})")
    else:
        # Model answered directly — also acceptable
        ok(f"Model answered directly (type={output_type})")
        print(f"\n  Model replied: \"{resp.get('output_text', '')[:80]}\"")


def test_3_codex_full_tool_loop(client: httpx.Client):
    """
    Codex CLI: full tool loop.
    Turn 1: User asks → model calls shell tool
    Turn 2: We send function_call_output → model gives final answer
    """
    separator("Test 3: Codex CLI — full tool loop (multi-turn)")

    # Turn 1: Ask something that should trigger a shell call
    resp1 = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": "Run 'echo hello' in the shell.",
        "tools": [SHELL_TOOL],
        "tool_choice": "auto",
        "stream": False,
    })

    assert_truthy("turn 1: id present", resp1.get("id", "").startswith("resp_"))
    output1 = resp1.get("output", [])
    assert_truthy("turn 1: output present", len(output1) > 0)

    first = output1[0]
    if first.get("type") != "function_call":
        # If model answered directly, that's OK but we can't test the full loop
        ok("Model answered directly — skipping tool loop test")
        print(f"\n  Model replied: \"{resp1.get('output_text', '')[:80]}\"")
        return

    assert_eq("turn 1: function_call.name", first.get("name"), "shell")
    call_id = first.get("call_id", "")
    assert_truthy("turn 1: call_id present", call_id)
    print(f"\n  Turn 1 — Model called: shell()")

    # Turn 2: Send function_call_output with the tool result
    resp2 = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": [
            {"role": "user", "content": "Run 'echo hello' in the shell."},
            {
                "type": "function_call",
                "name": first.get("name"),
                "arguments": first.get("arguments", "{}"),
                "call_id": call_id,
            },
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nhello\n",
            },
        ],
        "tools": [SHELL_TOOL],
        "tool_choice": "auto",
        "stream": False,
    })

    assert_truthy("turn 2: id present", resp2.get("id", "").startswith("resp_"))
    assert_eq("turn 2: status", resp2.get("status"), "completed")

    output2 = resp2.get("output", [])
    assert_truthy("turn 2: output present", len(output2) > 0)

    # Turn 2 should be a text message (the final answer), not another tool call
    if output2[0].get("type") == "message":
        ok("turn 2: model responded with text message")
        assert_nonempty_string("turn 2: output_text", resp2.get("output_text", ""))
    else:
        ok(f"turn 2: model responded with {output2[0].get('type')}")

    print(f"\n  Turn 2 — Model replied: \"{resp2.get('output_text', '')[:80]}\"")


def test_4_codex_apply_patch_tool(client: httpx.Client):
    """Codex CLI: model calls apply_patch to edit a file."""
    separator("Test 4: Codex CLI — apply_patch tool call")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": "Create a file called hello.py that prints 'Hello, World!'",
        "tools": [SHELL_TOOL, APPLY_PATCH_TOOL],
        "tool_choice": "auto",
        "stream": False,
    })

    assert_truthy("id present", resp.get("id", "").startswith("resp_"))
    output = resp.get("output", [])
    assert_truthy("output present", len(output) > 0)

    first = output[0]
    if first.get("type") == "function_call":
        name = first.get("name", "")
        assert_in("tool called", name, ["apply_patch", "shell"])
        assert_truthy("call_id present", first.get("call_id"))
        assert_nonempty_string("arguments", first.get("arguments", ""))
        print(f"\n  Model called: {name}({first.get('arguments', '')[:80]}...)")
    else:
        ok("Model answered directly")
        print(f"\n  Model replied: \"{resp.get('output_text', '')[:80]}\"")


def test_5_codex_streaming_tool_call(client: httpx.Client):
    """Codex CLI: streaming SSE events for a tool call response."""
    separator("Test 5: Codex CLI — streaming tool call SSE")

    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "catgpt-browser",
            "instructions": CODEX_SYSTEM_PROMPT,
            "input": "Check what version of python is installed. Use the shell tool.",
            "tools": [SHELL_TOOL],
            "tool_choice": "auto",
            "stream": True,
        },
        timeout=300.0,
    ) as r:
        r.raise_for_status()
        body = r.read().decode()

    events = parse_sse_events(body)
    event_types = [e["event"] for e in events]

    assert_truthy("received SSE events", len(events) > 0)
    assert_in("has response.created", "response.created", event_types)
    assert_in("has response.completed", "response.completed", event_types)

    # Check if it's a tool call or text response
    completed = next((e["data"] for e in events if e["event"] == "response.completed"), {})
    resp = completed.get("response", {})
    output = resp.get("output", [])

    if output and output[0].get("type") == "function_call":
        # Validate tool call SSE events
        assert_in("has output_item.added", "response.output_item.added", event_types)
        assert_in("has output_item.done", "response.output_item.done", event_types)

        # Should have function_call_arguments events
        if "response.function_call_arguments.delta" in event_types:
            ok("has function_call_arguments.delta")
            delta = next(
                (e["data"] for e in events
                 if e["event"] == "response.function_call_arguments.delta"), {}
            )
            assert_nonempty_string("delta content", delta.get("delta", ""))

        if "response.function_call_arguments.done" in event_types:
            ok("has function_call_arguments.done")
            done = next(
                (e["data"] for e in events
                 if e["event"] == "response.function_call_arguments.done"), {}
            )
            assert_eq("done.name", done.get("name"), "shell")
            assert_nonempty_string("done.arguments", done.get("arguments", ""))

        fc = output[0]
        print(f"\n  Model called: {fc.get('name')}() via streaming")
    else:
        ok("Model answered with text (no tool call)")
        print(f"\n  Model replied: \"{resp.get('output_text', '')[:80]}\"")


def test_6_codex_multiple_tools(client: httpx.Client):
    """Codex CLI: both shell and apply_patch tools available."""
    separator("Test 6: Codex CLI — multiple tools defined")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": "What is 2+2? Just answer directly, no tools needed.",
        "tools": [SHELL_TOOL, APPLY_PATCH_TOOL],
        "tool_choice": "auto",
        "stream": False,
    })

    assert_truthy("id present", resp.get("id", "").startswith("resp_"))
    assert_eq("status", resp.get("status"), "completed")

    # Should answer directly without using tools
    output = resp.get("output", [])
    assert_truthy("output present", len(output) > 0)

    # Both tools should be echoed
    tools = resp.get("tools", [])
    assert_truthy("both tools echoed", len(tools) >= 2)
    tool_names = {t.get("name") for t in tools}
    assert_in("shell in tools", "shell", tool_names)
    assert_in("apply_patch in tools", "apply_patch", tool_names)

    output_text = resp.get("output_text", "")
    assert_nonempty_string("output_text", output_text)
    print(f"\n  Model replied: \"{output_text}\"")


def test_7_codex_tool_choice_none(client: httpx.Client):
    """Codex CLI: tool_choice=none forces text response even with tools defined."""
    separator("Test 7: Codex CLI — tool_choice=none")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": "List files in /tmp. Describe what command you would use.",
        "tools": [SHELL_TOOL],
        "tool_choice": "none",
        "stream": False,
    })

    assert_truthy("id present", resp.get("id", "").startswith("resp_"))
    assert_eq("status", resp.get("status"), "completed")

    output = resp.get("output", [])
    assert_truthy("output present", len(output) > 0)

    # With tool_choice=none, should always be a text message
    first = output[0]
    assert_eq("output type is message", first.get("type"), "message")
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    print(f"\n  Model replied: \"{resp.get('output_text', '')[:80]}\"")


def test_8_codex_error_tool_output(client: httpx.Client):
    """Codex CLI: function_call_output with error — model should handle gracefully."""
    separator("Test 8: Codex CLI — tool output with error")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": CODEX_SYSTEM_PROMPT,
        "input": [
            {"role": "user", "content": "Check disk space on the machine."},
            {
                "type": "function_call",
                "name": "shell",
                "arguments": json.dumps({"command": ["df", "-h"]}),
                "call_id": "call_test_error_001",
            },
            {
                "type": "function_call_output",
                "call_id": "call_test_error_001",
                "output": "Exit code: 127\nWall time: 0.0 seconds\nOutput:\ndf: command not found\n",
            },
        ],
        "tools": [SHELL_TOOL],
        "tool_choice": "auto",
        "stream": False,
    })

    assert_truthy("id present", resp.get("id", "").startswith("resp_"))
    assert_eq("status", resp.get("status"), "completed")

    output = resp.get("output", [])
    assert_truthy("output present", len(output) > 0)

    # Model should respond with text explaining the error, or try another tool
    output_type = output[0].get("type")
    assert_in("output type", output_type, ["message", "function_call"])

    if output_type == "message":
        assert_nonempty_string("output_text", resp.get("output_text", ""))
        print(f"\n  Model replied: \"{resp.get('output_text', '')[:80]}\"")
    else:
        print(f"\n  Model called: {output[0].get('name')}() — trying alternative")


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Codex CLI /v1/responses tests")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="API base URL")
    parser.add_argument("--test", type=int, help="Run a specific test number (1-8)")
    parser.add_argument("--api-key", default=API_KEY, help="API bearer token")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    client = httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {args.api_key}"},
        timeout=httpx.Timeout(300.0, connect=10.0),
    )

    ALL_TESTS = [
        (1, test_1_codex_simple_question),
        (2, test_2_codex_tool_call_shell),
        (3, test_3_codex_full_tool_loop),
        (4, test_4_codex_apply_patch_tool),
        (5, test_5_codex_streaming_tool_call),
        (6, test_6_codex_multiple_tools),
        (7, test_7_codex_tool_choice_none),
        (8, test_8_codex_error_tool_output),
    ]

    BROWSER_TESTS = {1, 2, 3, 4, 5, 6, 7, 8}

    print("\n" + "=" * 64)
    print("  CatGPT Gateway — Codex CLI Compatibility Test Suite")
    print("=" * 64)
    print(f"  Base URL : {base_url}")
    print(f"  Auth     : Bearer {args.api_key[:4]}{'*' * (len(args.api_key) - 4)}")

    # Check server reachability
    try:
        r = client.get("/v1/models", timeout=10.0)
        if r.status_code == 200:
            print(f"  Server   : OK")
        else:
            print(f"\n  ⚠️  /v1/models returned {r.status_code}")
    except httpx.ConnectError:
        print(f"\n  ❌ Cannot connect to {base_url}")
        sys.exit(1)

    start_time = time.time()
    last_was_browser = False

    for num, test_fn in ALL_TESTS:
        if args.test is not None and num != args.test:
            continue

        is_browser = num in BROWSER_TESTS
        if is_browser and last_was_browser:
            print("\n  ⏳ Waiting 5s between browser tests...")
            time.sleep(5)

        try:
            test_fn(client)
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.text
            except httpx.ResponseNotRead:
                body = "(streaming response not read)"
            fail(f"Test {num} HTTP error: {e.response.status_code} — {body[:200]}")
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            fail(f"Test {num} timeout: {e}")
        except Exception as e:
            fail(f"Test {num} exception: {e}")
            traceback.print_exc()

        last_was_browser = is_browser

    client.close()
    elapsed = time.time() - start_time

    print(f"\n{'=' * 64}")
    print(f"  RESULTS: {PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    print(f"  Time: {elapsed:.1f}s")
    print(f"{'=' * 64}")

    if ERRORS:
        print("\n  Failures:")
        for err in ERRORS:
            print(f"    • {err}")

    print()
    sys.exit(1 if FAILED > 0 else 0)


if __name__ == "__main__":
    main()
