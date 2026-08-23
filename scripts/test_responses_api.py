#!/usr/bin/env python3
"""
Comprehensive test suite for POST /v1/responses endpoint.

Validates the full Responses API contract including:
  1. Non-streaming with string input
  2. Non-streaming with message array input
  3. Non-streaming with instructions (system prompt)
  4. Non-streaming with developer role (maps to system)
  5. Non-streaming with input_text content parts
  6. Streaming SSE event sequence
  7. Streaming SSE event structure validation
  8. Tool/function call definitions (flat format)
  9. Error cases (empty input, missing auth)
  10. Response schema field-level validation

Prerequisites:
  - CatGPT API server running: python -m src.api.server
  - API_TOKEN must match .env (default: dummy123)

Usage:
  python scripts/test_responses_api.py
  python scripts/test_responses_api.py --base-url http://host:port
  python scripts/test_responses_api.py --test 1    # run a specific test
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

# Load .env if dotenv is available
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


def skip(msg: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  ⏭️  {msg}")


def assert_eq(label: str, actual, expected):
    if actual == expected:
        ok(f"{label}: {actual!r}")
    else:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def assert_in(label: str, value, container):
    if value in container:
        ok(f"{label}: {value!r} found")
    else:
        fail(f"{label}: {value!r} not in {container!r}")


def assert_type(label: str, value, expected_type):
    if isinstance(value, expected_type):
        ok(f"{label}: type is {type(value).__name__}")
    else:
        fail(f"{label}: expected {expected_type.__name__}, got {type(value).__name__}")


def assert_truthy(label: str, value):
    if value:
        ok(f"{label}")
    else:
        fail(f"{label}: value is falsy: {value!r}")


def assert_nonempty_string(label: str, value):
    if isinstance(value, str) and len(value) > 0:
        ok(f"{label}: \"{value[:60]}{'...' if len(value) > 60 else ''}\"")
    else:
        fail(f"{label}: expected non-empty string, got {value!r}")


def post_responses(client: httpx.Client, body: dict, stream: bool = False):
    """POST to /v1/responses. Returns parsed JSON or raw response for streaming."""
    if stream:
        return client.post("/v1/responses", json=body, timeout=180.0)
    r = client.post("/v1/responses", json=body, timeout=180.0)
    r.raise_for_status()
    return r.json()


def parse_sse_events(text: str) -> list[dict]:
    """Parse SSE event stream text into list of {event, data} dicts."""
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

    # Handle trailing event without final blank line
    if current_event is not None and current_data_lines:
        data_str = "\n".join(current_data_lines)
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            data = data_str
        events.append({"event": current_event, "data": data})

    return events


# ── Response Schema Validator ───────────────────────────────────

def validate_response_object(resp: dict, label: str = "response"):
    """Validate that a response object has all required fields with correct types."""
    # Required string fields
    assert_truthy(f"{label}.id starts with resp_", resp.get("id", "").startswith("resp_"))
    assert_eq(f"{label}.object", resp.get("object"), "response")
    assert_in(f"{label}.status", resp.get("status"), ["completed", "in_progress", "failed", "incomplete"])
    assert_type(f"{label}.created_at", resp.get("created_at"), int)
    assert_type(f"{label}.model", resp.get("model"), str)

    # Output array
    assert_type(f"{label}.output", resp.get("output"), list)

    # Metadata
    assert_type(f"{label}.metadata", resp.get("metadata"), dict)

    # Tools echo
    assert_type(f"{label}.tools", resp.get("tools"), list)

    # Text format
    text_field = resp.get("text")
    if text_field is not None:
        assert_type(f"{label}.text", text_field, dict)
        assert_eq(f"{label}.text.format.type", text_field.get("format", {}).get("type"), "text")


def validate_output_message(item: dict, label: str = "output[0]"):
    """Validate a message output item."""
    assert_eq(f"{label}.type", item.get("type"), "message")
    assert_eq(f"{label}.role", item.get("role"), "assistant")
    assert_eq(f"{label}.status", item.get("status"), "completed")
    assert_type(f"{label}.content", item.get("content"), list)

    if item.get("content"):
        part = item["content"][0]
        assert_eq(f"{label}.content[0].type", part.get("type"), "output_text")
        assert_type(f"{label}.content[0].text", part.get("text"), str)
        assert_truthy(f"{label}.content[0].text is non-empty", len(part.get("text", "")) > 0)
        assert_type(f"{label}.content[0].annotations", part.get("annotations"), list)


def validate_usage(usage: dict, label: str = "usage"):
    """Validate usage object."""
    assert_type(f"{label}.input_tokens", usage.get("input_tokens"), int)
    assert_type(f"{label}.output_tokens", usage.get("output_tokens"), int)
    assert_type(f"{label}.total_tokens", usage.get("total_tokens"), int)
    assert_truthy(
        f"{label}.total_tokens = input + output",
        usage.get("total_tokens") == usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    )
    details = usage.get("output_tokens_details")
    assert_type(f"{label}.output_tokens_details", details, dict)


# ── Tests ───────────────────────────────────────────────────────

def test_1_string_input(client: httpx.Client):
    """Non-streaming: simple string input."""
    separator("Test 1: Non-streaming — string input")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": "What is the capital of Japan? Reply with the city name only.",
        "stream": False,
    })

    validate_response_object(resp)
    assert_eq("status", resp["status"], "completed")
    assert_type("completed_at", resp.get("completed_at"), int)
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    # Validate output array
    assert_truthy("output has items", len(resp["output"]) > 0)
    validate_output_message(resp["output"][0])

    # output_text should match the text inside output[0].content[0].text
    inner_text = resp["output"][0]["content"][0]["text"]
    assert_eq("output_text matches output[0]", resp["output_text"], inner_text)

    # Usage
    assert_truthy("usage present", resp.get("usage") is not None)
    validate_usage(resp["usage"])

    print(f"\n  Model replied: \"{resp['output_text']}\"")


def test_2_message_array_input(client: httpx.Client):
    """Non-streaming: message array input (role + content)."""
    separator("Test 2: Non-streaming — message array input")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": [
            {"role": "user", "content": "What is 7 * 8? Reply with just the number."},
        ],
        "stream": False,
    })

    validate_response_object(resp)
    assert_eq("status", resp["status"], "completed")
    assert_truthy("output has items", len(resp["output"]) > 0)
    validate_output_message(resp["output"][0])
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    print(f"\n  Model replied: \"{resp['output_text']}\"")


def test_3_instructions(client: httpx.Client):
    """Non-streaming: instructions field (system prompt)."""
    separator("Test 3: Non-streaming — instructions (system prompt)")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "instructions": "You must respond in exactly 3 words. No more, no less.",
        "input": "Describe the ocean.",
        "stream": False,
    })

    validate_response_object(resp)
    assert_eq("instructions echoed", resp.get("instructions"),
              "You must respond in exactly 3 words. No more, no less.")
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    print(f"\n  Model replied: \"{resp['output_text']}\"")


def test_4_developer_role(client: httpx.Client):
    """Non-streaming: developer role in input (should map to system)."""
    separator("Test 4: Non-streaming — developer role input")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": [
            {"role": "developer", "content": "Always respond in ALL CAPS."},
            {"role": "user", "content": "Say hi."},
        ],
        "stream": False,
    })

    validate_response_object(resp)
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    print(f"\n  Model replied: \"{resp['output_text']}\"")


def test_5_input_text_content_parts(client: httpx.Client):
    """Non-streaming: input with content parts using input_text type."""
    separator("Test 5: Non-streaming — input_text content parts")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What color is the sky? One word answer."},
                ],
            },
        ],
        "stream": False,
    })

    validate_response_object(resp)
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    print(f"\n  Model replied: \"{resp['output_text']}\"")


def test_6_streaming_event_sequence(client: httpx.Client):
    """Streaming: validate complete SSE event sequence."""
    separator("Test 6: Streaming — SSE event sequence")

    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "catgpt-browser",
            "input": "What is 1+1? Reply with the number only.",
            "stream": True,
        },
        timeout=300.0,
    ) as r:
        r.raise_for_status()
        body = r.read().decode()

    events = parse_sse_events(body)
    event_types = [e["event"] for e in events]

    print(f"  Received {len(events)} SSE events")
    for i, e in enumerate(events):
        seq = e["data"].get("sequence_number", "?") if isinstance(e["data"], dict) else "?"
        print(f"    [{i}] seq={seq} {e['event']}")

    # Validate required event order
    REQUIRED_SEQUENCE = [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]

    assert_truthy(f"at least {len(REQUIRED_SEQUENCE)} events", len(events) >= len(REQUIRED_SEQUENCE))

    # Check sequence order (events should appear in this order)
    last_idx = -1
    for expected_type in REQUIRED_SEQUENCE:
        try:
            idx = event_types.index(expected_type)
        except ValueError:
            fail(f"Missing required event: {expected_type}")
            continue
        if idx > last_idx:
            ok(f"Event order: {expected_type} at position {idx}")
            last_idx = idx
        else:
            fail(f"Event order: {expected_type} at position {idx} but expected after {last_idx}")

    # Validate sequence_number is monotonically increasing
    seq_numbers = []
    for e in events:
        if isinstance(e["data"], dict):
            seq_numbers.append(e["data"].get("sequence_number", -1))
    is_monotonic = all(seq_numbers[i] < seq_numbers[i + 1] for i in range(len(seq_numbers) - 1))
    if is_monotonic:
        ok(f"Sequence numbers monotonically increasing: {seq_numbers}")
    else:
        fail(f"Sequence numbers NOT monotonic: {seq_numbers}")


def test_7_streaming_event_content(client: httpx.Client):
    """Streaming: validate individual event payloads."""
    separator("Test 7: Streaming — event payload validation")

    with client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "catgpt-browser",
            "input": "Say the word 'test'. Nothing else.",
            "stream": True,
        },
        timeout=300.0,
    ) as r:
        r.raise_for_status()
        body = r.read().decode()

    events = parse_sse_events(body)
    events_by_type = {e["event"]: e["data"] for e in events}

    # response.created
    created = events_by_type.get("response.created", {})
    created_resp = created.get("response", {})
    assert_eq("created.status", created_resp.get("status"), "in_progress")
    assert_eq("created.output", created_resp.get("output"), [])
    assert_eq("created.output_text", created_resp.get("output_text"), None)
    assert_eq("created.usage", created_resp.get("usage"), None)
    assert_truthy("created.id starts with resp_", created_resp.get("id", "").startswith("resp_"))

    # response.output_item.added
    item_added = events_by_type.get("response.output_item.added", {})
    assert_eq("item_added.output_index", item_added.get("output_index"), 0)
    item = item_added.get("item", {})
    assert_eq("item_added.item.type", item.get("type"), "message")
    assert_eq("item_added.item.role", item.get("role"), "assistant")
    assert_eq("item_added.item.status", item.get("status"), "in_progress")
    assert_eq("item_added.item.content", item.get("content"), [])

    # response.content_part.added
    part_added = events_by_type.get("response.content_part.added", {})
    assert_eq("part_added.content_index", part_added.get("content_index"), 0)
    part = part_added.get("part", {})
    assert_eq("part_added.part.type", part.get("type"), "output_text")
    assert_eq("part_added.part.text", part.get("text"), "")

    # response.output_text.delta
    delta_evt = events_by_type.get("response.output_text.delta", {})
    assert_eq("delta.output_index", delta_evt.get("output_index"), 0)
    assert_eq("delta.content_index", delta_evt.get("content_index"), 0)
    assert_nonempty_string("delta.delta", delta_evt.get("delta", ""))

    # response.output_text.done
    text_done = events_by_type.get("response.output_text.done", {})
    assert_nonempty_string("text_done.text", text_done.get("text", ""))
    assert_eq("delta matches done text", delta_evt.get("delta"), text_done.get("text"))

    # response.content_part.done
    part_done = events_by_type.get("response.content_part.done", {})
    assert_eq("part_done.part.text matches", part_done.get("part", {}).get("text"), text_done.get("text"))

    # response.output_item.done
    item_done = events_by_type.get("response.output_item.done", {})
    done_item = item_done.get("item", {})
    assert_eq("item_done.item.status", done_item.get("status"), "completed")
    assert_truthy("item_done.item.content non-empty", len(done_item.get("content", [])) > 0)

    # response.completed
    completed = events_by_type.get("response.completed", {})
    completed_resp = completed.get("response", {})
    assert_eq("completed.status", completed_resp.get("status"), "completed")
    assert_type("completed.completed_at", completed_resp.get("completed_at"), int)
    assert_truthy("completed.usage present", completed_resp.get("usage") is not None)
    assert_truthy("completed.output non-empty", len(completed_resp.get("output", [])) > 0)
    assert_nonempty_string("completed.output_text", completed_resp.get("output_text", ""))

    # IDs should be consistent
    item_id_added = item_added.get("item", {}).get("id")
    item_id_delta = delta_evt.get("item_id")
    item_id_done = item_done.get("item", {}).get("id")
    assert_eq("item_id consistent (added vs delta)", item_id_added, item_id_delta)
    assert_eq("item_id consistent (added vs done)", item_id_added, item_id_done)

    # Created response ID matches completed response ID
    assert_eq(
        "response.id consistent (created vs completed)",
        created_resp.get("id"),
        completed_resp.get("id"),
    )


def test_8_tools_definition(client: httpx.Client):
    """Non-streaming: tools are accepted and echoed in flat format."""
    separator("Test 8: Non-streaming — tools definition echo")

    tools = [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    ]

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": "What's the weather in Paris?",
        "tools": tools,
        "stream": False,
    })

    validate_response_object(resp)

    # Tools should be echoed back
    assert_truthy("tools echoed in response", len(resp.get("tools", [])) > 0)
    echoed_tool = resp["tools"][0]
    assert_eq("echoed tool.name", echoed_tool.get("name"), "get_weather")
    assert_eq("echoed tool.type", echoed_tool.get("type"), "function")

    # Response should be either a text answer or a function_call
    output = resp.get("output", [])
    assert_truthy("output has items", len(output) > 0)

    first_output = output[0]
    output_type = first_output.get("type")
    assert_in("output type", output_type, ["message", "function_call"])

    if output_type == "function_call":
        assert_eq("function_call.name", first_output.get("name"), "get_weather")
        assert_nonempty_string("function_call.arguments", first_output.get("arguments", ""))
        assert_truthy("function_call.call_id present", first_output.get("call_id"))
        assert_eq("function_call.status", first_output.get("status"), "completed")
        print(f"\n  Model called tool: {first_output['name']}({first_output['arguments']})")
    else:
        print(f"\n  Model answered directly: \"{resp.get('output_text', '')[:80]}\"")


def test_9_error_empty_input(client: httpx.Client):
    """Error case: empty input should return 400."""
    separator("Test 9: Error — empty input")

    r = client.post("/v1/responses", json={
        "model": "catgpt-browser",
        "input": "",
        "stream": False,
    })

    assert_eq("status_code", r.status_code, 400)
    body = r.json()
    assert_truthy("error detail present", "detail" in body)
    print(f"  Error message: {body.get('detail')}")


def test_10_error_no_auth(base_url: str):
    """Error case: missing auth token should return 401."""
    separator("Test 10: Error — missing auth token")

    # Create client WITHOUT auth header
    no_auth_client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        r = no_auth_client.post("/v1/responses", json={
            "model": "catgpt-browser",
            "input": "hello",
            "stream": False,
        })
        assert_eq("status_code", r.status_code, 401)
    finally:
        no_auth_client.close()


def test_11_metadata_passthrough(client: httpx.Client):
    """Non-streaming: metadata and previous_response_id are accepted and echoed."""
    separator("Test 11: Non-streaming — metadata & previous_response_id")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": "Say OK.",
        "metadata": {"user_id": "test-123", "session": "abc"},
        "previous_response_id": "resp_fake_previous_id",
        "stream": False,
    })

    validate_response_object(resp)
    assert_eq("metadata.user_id", resp.get("metadata", {}).get("user_id"), "test-123")
    assert_eq("metadata.session", resp.get("metadata", {}).get("session"), "abc")
    assert_eq("previous_response_id", resp.get("previous_response_id"), "resp_fake_previous_id")

    print(f"\n  Model replied: \"{resp.get('output_text', '')}\"")


def test_12_multi_message_conversation(client: httpx.Client):
    """Non-streaming: multi-turn conversation in input array."""
    separator("Test 12: Non-streaming — multi-turn conversation")

    resp = post_responses(client, {
        "model": "catgpt-browser",
        "input": [
            {"role": "user", "content": "Remember this number: 42"},
            {"role": "assistant", "content": "Got it, I'll remember the number 42."},
            {"role": "user", "content": "What number did I ask you to remember? Reply with just the number."},
        ],
        "stream": False,
    })

    validate_response_object(resp)
    assert_nonempty_string("output_text", resp.get("output_text", ""))

    print(f"\n  Model replied: \"{resp['output_text']}\"")


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test /v1/responses endpoint")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help="API base URL")
    parser.add_argument("--test", type=int, help="Run a specific test number (1-12)")
    parser.add_argument("--api-key", default=API_KEY, help="API bearer token")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")

    client = httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {args.api_key}"},
        timeout=httpx.Timeout(180.0, connect=10.0),
    )

    ALL_TESTS = [
        # Error tests first — they don't hit the browser
        (9, test_9_error_empty_input),
        (10, test_10_error_no_auth),
        # Non-streaming tests
        (1, test_1_string_input),
        (2, test_2_message_array_input),
        (3, test_3_instructions),
        (4, test_4_developer_role),
        (5, test_5_input_text_content_parts),
        # Streaming tests (need breathing room after non-streaming)
        (6, test_6_streaming_event_sequence),
        (7, test_7_streaming_event_content),
        # Tool calling + metadata + multi-turn
        (8, test_8_tools_definition),
        (11, test_11_metadata_passthrough),
        (12, test_12_multi_message_conversation),
    ]

    # Tests that hit the browser — need a delay between them
    BROWSER_TESTS = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12}

    print("\n" + "=" * 64)
    print("  CatGPT Gateway — /v1/responses API Test Suite")
    print("=" * 64)
    print(f"  Base URL : {base_url}")
    print(f"  Auth     : Bearer {args.api_key[:4]}{'*' * (len(args.api_key) - 4)}")

    # Check server is reachable
    try:
        r = client.get("/health")
        if r.status_code != 200:
            print(f"\n  ⚠️  /health returned {r.status_code} — server may not be ready")
        else:
            print(f"  Health   : OK")
    except httpx.ConnectError:
        print(f"\n  ❌ Cannot connect to {base_url}")
        print("     Start the server first: python -m src.api.server")
        sys.exit(1)

    start_time = time.time()
    last_was_browser = False

    for num, test_fn in ALL_TESTS:
        if args.test is not None and num != args.test:
            continue

        # Delay between browser-hitting tests to prevent ChatGPT rate limits
        is_browser = num in BROWSER_TESTS
        if is_browser and last_was_browser:
            print("\n  ⏳ Waiting 5s between browser tests...")
            time.sleep(5)

        try:
            if test_fn == test_10_error_no_auth:
                test_fn(base_url)
            else:
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

    # Summary
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
