#!/usr/bin/env python3
"""
Test script for the OpenAI-compatible Image Generation endpoint.

Tests the POST /v1/images/generations endpoint by generating images
via the CatGPT API and verifying the response format matches OpenAI's spec.

Tests:
  1. Generate a single image (b64_json format)
  2. Generate an image and save to disk
  3. Generate an image (url format — returns local file path)
  4. Use the OpenAI SDK client.images.generate()

Prerequisites:
  - CatGPT API server running: python -m src.api.server
  - OR Docker: docker compose up --build -d catgpt
  - pip install openai requests

Usage:
  python scripts/test_image_generation.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

try:
    from openai import OpenAI
except ImportError:
    print("WARNING: openai SDK not installed — Test 4 will be skipped")
    OpenAI = None


# ── Configuration ───────────────────────────────────────────────

BASE_URL = "http://localhost:8000"
API_KEY = "dummy123"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "downloads" / "images" / "test_generated"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
}


# ── Helpers ─────────────────────────────────────────────────────

def separator(title: str) -> None:
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def save_b64_image(b64_data: str, filename: str) -> str:
    """Save a base64-encoded image to disk. Returns the file path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename
    img_bytes = base64.b64decode(b64_data)
    filepath.write_bytes(img_bytes)
    size_kb = len(img_bytes) / 1024
    print(f"  Saved: {filepath} ({size_kb:.1f} KB)")
    return str(filepath)


# ── Tests ───────────────────────────────────────────────────────

def test_1_basic_image_generation():
    """Test 1: Generate a single image via raw HTTP request (b64_json)."""
    separator("Test 1: Basic Image Generation (b64_json)")

    payload = {
        "prompt": "A cute orange tabby cat sitting on a keyboard, digital art style",
        "model": "dall-e-3",
        "n": 1,
        "size": "1024x1024",
        "response_format": "b64_json",
    }

    print(f"  Prompt: {payload['prompt']}")
    print(f"  Sending request...")
    start = time.time()

    resp = requests.post(f"{BASE_URL}/v1/images/generations", headers=HEADERS, json=payload, timeout=180)
    elapsed = time.time() - start

    print(f"  Status: {resp.status_code} ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"  FAILED: {resp.text[:500]}")
        return False

    data = resp.json()
    print(f"  Response keys: {list(data.keys())}")

    # Validate response structure
    assert "created" in data, "Missing 'created' field"
    assert "data" in data, "Missing 'data' field"
    assert isinstance(data["data"], list), "'data' should be a list"
    assert len(data["data"]) >= 1, "'data' should have at least 1 image"

    img = data["data"][0]
    assert "b64_json" in img and img["b64_json"], "Missing 'b64_json' in image data"

    # Validate it's valid base64
    try:
        img_bytes = base64.b64decode(img["b64_json"])
        size_kb = len(img_bytes) / 1024
        print(f"  Image size: {size_kb:.1f} KB")
    except Exception as e:
        print(f"  FAILED: Invalid base64 data: {e}")
        return False

    # Check for revised_prompt (optional but nice)
    if img.get("revised_prompt"):
        print(f"  Revised prompt: {img['revised_prompt'][:100]}")

    # Save the image
    save_b64_image(img["b64_json"], "test1_cat_keyboard.png")

    print(f"\n  PASSED ✓ (generated 1 image in {elapsed:.1f}s)")
    return True


def test_2_save_generated_image():
    """Test 2: Generate an image with a different prompt and verify save."""
    separator("Test 2: Generate & Save Image")

    payload = {
        "prompt": "A futuristic cyberpunk cityscape at night with neon lights, 4k quality",
        "model": "dall-e-3",
        "n": 1,
        "size": "1024x1024",
        "quality": "hd",
        "response_format": "b64_json",
    }

    print(f"  Prompt: {payload['prompt']}")
    print(f"  Quality: {payload['quality']}")
    print(f"  Sending request...")
    start = time.time()

    resp = requests.post(f"{BASE_URL}/v1/images/generations", headers=HEADERS, json=payload, timeout=180)
    elapsed = time.time() - start

    print(f"  Status: {resp.status_code} ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"  FAILED: {resp.text[:500]}")
        return False

    data = resp.json()
    img = data["data"][0]

    if not img.get("b64_json"):
        print(f"  FAILED: No b64_json in response")
        return False

    filepath = save_b64_image(img["b64_json"], "test2_cyberpunk_city.png")

    # Verify the saved file exists and has content
    saved = Path(filepath)
    assert saved.exists(), f"Saved file does not exist: {filepath}"
    assert saved.stat().st_size > 1000, f"Saved file too small: {saved.stat().st_size} bytes"

    print(f"  File verified: {saved.stat().st_size} bytes")
    print(f"\n  PASSED ✓ ({elapsed:.1f}s)")
    return True


def test_3_url_format():
    """Test 3: Generate an image with response_format='url'."""
    separator("Test 3: Image Generation (url format)")

    payload = {
        "prompt": "A simple watercolor painting of a mountain landscape with a lake",
        "model": "dall-e-3",
        "n": 1,
        "size": "1024x1024",
        "response_format": "url",
    }

    print(f"  Prompt: {payload['prompt']}")
    print(f"  Response format: url")
    print(f"  Sending request...")
    start = time.time()

    resp = requests.post(f"{BASE_URL}/v1/images/generations", headers=HEADERS, json=payload, timeout=180)
    elapsed = time.time() - start

    print(f"  Status: {resp.status_code} ({elapsed:.1f}s)")

    if resp.status_code != 200:
        print(f"  FAILED: {resp.text[:500]}")
        return False

    data = resp.json()
    img = data["data"][0]

    assert "url" in img and img["url"], "Missing 'url' in image data"
    print(f"  Image URL/path: {img['url']}")

    # If it's a local path, verify the file exists
    if not img["url"].startswith("http"):
        path = Path(img["url"])
        if path.exists():
            print(f"  File exists: {path.stat().st_size} bytes")
        else:
            print(f"  WARNING: Local path does not exist: {img['url']}")

    if img.get("revised_prompt"):
        print(f"  Revised prompt: {img['revised_prompt'][:100]}")

    print(f"\n  PASSED ✓ ({elapsed:.1f}s)")
    return True


def test_4_openai_sdk():
    """Test 4: Use the official OpenAI Python SDK."""
    separator("Test 4: OpenAI SDK — client.images.generate()")

    if OpenAI is None:
        print("  SKIPPED — openai SDK not installed")
        return True

    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)

    prompt = "A photorealistic golden retriever puppy wearing sunglasses on a beach"
    print(f"  Prompt: {prompt}")
    print(f"  Using OpenAI SDK client.images.generate()")
    print(f"  Sending request...")
    start = time.time()

    try:
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            n=1,
            size="1024x1024",
            response_format="b64_json",
        )
    except Exception as e:
        print(f"  FAILED: {e}")
        return False

    elapsed = time.time() - start
    print(f"  Response received ({elapsed:.1f}s)")

    # Validate SDK response object
    assert response.data, "response.data is empty"
    assert len(response.data) >= 1, "Expected at least 1 image"

    img = response.data[0]
    assert img.b64_json, "Missing b64_json in response"

    img_bytes = base64.b64decode(img.b64_json)
    size_kb = len(img_bytes) / 1024
    print(f"  Image size: {size_kb:.1f} KB")

    if img.revised_prompt:
        print(f"  Revised prompt: {img.revised_prompt[:100]}")

    # Save it
    save_b64_image(img.b64_json, "test4_puppy_sunglasses.png")

    print(f"\n  PASSED ✓ ({elapsed:.1f}s)")
    return True


# ── Main ────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 70)
    print("  CatGPT — Image Generation API Test Suite")
    print("  Endpoint: POST /v1/images/generations")
    print(f"  Server:   {BASE_URL}")
    print("=" * 70)

    # Quick health check
    try:
        health = requests.get(f"{BASE_URL}/healthz", timeout=5)
        if health.status_code != 200:
            print(f"\n  ERROR: Server health check failed ({health.status_code})")
            sys.exit(1)
        print(f"\n  Health check: OK")
    except requests.ConnectionError:
        print(f"\n  ERROR: Cannot connect to {BASE_URL}")
        print("  Start the server: python -m src.api.server")
        print("  Or Docker: docker compose up --build -d catgpt")
        sys.exit(1)

    # Verify auth works
    try:
        models = requests.get(f"{BASE_URL}/v1/models", headers=HEADERS, timeout=5)
        if models.status_code == 401:
            print(f"  ERROR: Auth failed — check API_KEY (current: {API_KEY})")
            sys.exit(1)
        print(f"  Auth check: OK\n")
    except Exception:
        pass

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = {}
    tests = [
        ("Test 1: Basic b64_json generation", test_1_basic_image_generation),
        ("Test 2: Generate & save to disk", test_2_save_generated_image),
        ("Test 3: URL format response", test_3_url_format),
        ("Test 4: OpenAI SDK integration", test_4_openai_sdk),
    ]

    for name, test_fn in tests:
        try:
            passed = test_fn()
            results[name] = "PASSED" if passed else "FAILED"
        except Exception as e:
            print(f"\n  EXCEPTION: {e}")
            results[name] = "ERROR"
        # Brief pause between tests
        time.sleep(3)

    # ── Summary ─────────────────────────────────────────────
    separator("Test Results Summary")
    all_passed = True
    for name, result in results.items():
        icon = "✓" if result == "PASSED" else "✗"
        print(f"  {icon} {name}: {result}")
        if result != "PASSED":
            all_passed = False

    print()
    if all_passed:
        print("  All tests passed! ✓")
    else:
        print("  Some tests failed. Check output above for details.")

    # Show generated files
    if OUTPUT_DIR.exists():
        files = list(OUTPUT_DIR.glob("*.png"))
        if files:
            print(f"\n  Generated images saved to: {OUTPUT_DIR}")
            for f in sorted(files):
                print(f"    - {f.name} ({f.stat().st_size / 1024:.1f} KB)")

    print()
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
