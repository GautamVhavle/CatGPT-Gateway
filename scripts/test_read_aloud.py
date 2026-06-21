#!/usr/bin/env python3
"""
Manual test script for read-aloud audio capture.

Tests:
  1. POST /chat with read_aloud=true
  2. POST /v1/chat/completions with read_aloud=true

Prerequisites:
  - CatGPT API server running: python -m src.api.server
  - OR Docker: docker compose up --build -d catgpt
  - Logged in to ChatGPT in the browser session
  - pip install requests

Usage:
  python scripts/test_read_aloud.py
  python scripts/test_read_aloud.py --message "Lee esta frase en voz alta."
  python scripts/test_read_aloud.py --skip-openai
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)


BASE_URL = os.environ.get("CATGPT_BASE_URL", "http://localhost:8000")
API_KEY = os.environ.get("CATGPT_API_KEY", "dummy123")
MODEL = os.environ.get("CATGPT_MODEL", "catgpt-browser")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}

DEFAULT_MESSAGE = (
    "Reply with exactly this short sentence: "
    "The read aloud test is working correctly."
)


def separator(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}\n")


def check_server() -> bool:
    """Verify the API server is reachable before running slow audio tests."""
    try:
        resp = requests.get(f"{BASE_URL}/healthz", timeout=10)
    except requests.RequestException as exc:
        print(f"  FAILED: server is not reachable at {BASE_URL}: {exc}")
        return False

    if resp.status_code != 200:
        print(f"  FAILED: /healthz returned {resp.status_code}: {resp.text[:300]}")
        return False

    print(f"  Server OK: {BASE_URL}")
    return True


def validate_audio_payload(audio: dict | None) -> bool:
    """Validate audio metadata and local file availability."""
    if not audio:
        print("  FAILED: response did not include audio metadata")
        return False

    local_path = audio.get("local_path", "")
    mime_type = audio.get("mime_type", "")
    size_bytes = audio.get("size_bytes", 0)

    print(f"  Audio path: {local_path}")
    print(f"  MIME type:  {mime_type or '(missing)'}")
    print(f"  Size:       {size_bytes} bytes")

    if not local_path:
        print("  FAILED: audio.local_path is empty")
        return False

    path = Path(local_path)
    if not path.exists():
        print(f"  FAILED: audio file does not exist: {path}")
        return False

    actual_size = path.stat().st_size
    if actual_size <= 0:
        print("  FAILED: audio file is empty")
        return False

    if size_bytes and size_bytes != actual_size:
        print(f"  WARNING: metadata size ({size_bytes}) differs from file size ({actual_size})")

    print(f"  File exists: {actual_size} bytes")
    return True


def test_chat_endpoint(message: str) -> bool:
    """Test the native POST /chat endpoint."""
    separator("Test 1: Native /chat read_aloud")

    payload = {
        "message": message,
        "read_aloud": True,
    }

    print(f"  Message: {message}")
    print("  Sending request. This can take 30-90 seconds...")
    start = time.time()

    try:
        resp = requests.post(f"{BASE_URL}/chat", headers=HEADERS, json=payload, timeout=180)
    except requests.RequestException as exc:
        print(f"  FAILED: request error: {exc}")
        return False

    elapsed = time.time() - start
    print(f"  Status: {resp.status_code} ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"  FAILED: {resp.text[:800]}")
        return False

    data = resp.json()
    print(f"  Response text: {data.get('message', '')[:200]}")
    print(f"  has_audio: {data.get('has_audio')}")

    if "has_audio" not in data:
        print("  FAILED: response does not include the has_audio field")
        print("  Tip: restart/rebuild the CatGPT server so it runs the new read_aloud code.")
        return False

    if not data.get("has_audio"):
        print("  FAILED: has_audio is false")
        print("  Tip: confirm ChatGPT shows More actions -> Read aloud for the latest response.")
        return False

    ok = validate_audio_payload(data.get("audio"))
    if ok:
        print("\n  PASSED: /chat generated and downloaded read-aloud audio")
    return ok


def test_openai_endpoint(message: str) -> bool:
    """Test POST /v1/chat/completions with the custom read_aloud flag."""
    separator("Test 2: OpenAI-compatible /v1/chat/completions read_aloud")

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": message}],
        "read_aloud": True,
    }

    print(f"  Model: {MODEL}")
    print(f"  Message: {message}")
    print("  Sending request. This can take 30-90 seconds...")
    start = time.time()

    try:
        resp = requests.post(
            f"{BASE_URL}/v1/chat/completions",
            headers=HEADERS,
            json=payload,
            timeout=180,
        )
    except requests.RequestException as exc:
        print(f"  FAILED: request error: {exc}")
        return False

    elapsed = time.time() - start
    print(f"  Status: {resp.status_code} ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"  FAILED: {resp.text[:800]}")
        return False

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        print("  FAILED: response has no choices")
        return False

    message_data = choices[0].get("message", {})
    print(f"  Response text: {(message_data.get('content') or '')[:200]}")

    if "audio" not in message_data:
        print("  FAILED: response does not include message.audio")
        print("  Tip: restart/rebuild the CatGPT server so it runs the new read_aloud code.")
        return False

    ok = validate_audio_payload(message_data.get("audio"))
    if ok:
        print("\n  PASSED: /v1/chat/completions generated and downloaded read-aloud audio")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Test CatGPT read-aloud audio capture")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="Message to send")
    parser.add_argument("--skip-chat", action="store_true", help="Skip native /chat endpoint")
    parser.add_argument("--skip-openai", action="store_true", help="Skip /v1/chat/completions endpoint")
    args = parser.parse_args()

    separator("Read-Aloud Audio Test")
    print(f"  Base URL: {BASE_URL}")
    print(f"  API key:  {'set' if API_KEY else '(empty)'}")

    if not check_server():
        return 1

    results: list[tuple[str, bool]] = []
    if not args.skip_chat:
        results.append(("/chat", test_chat_endpoint(args.message)))
    if not args.skip_openai:
        results.append(("/v1/chat/completions", test_openai_endpoint(args.message)))

    separator("Summary")
    passed = 0
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status} - {name}")
        passed += int(ok)

    total = len(results)
    print(f"\n  Result: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
