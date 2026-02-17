#!/usr/bin/env python3
"""
Image Generation Test — validates image detection, extraction, and download.

Tests:
  1. Simple image generation (single image)
  2. Image with follow-up text question (mixed response check)
  3. Verify image files exist on disk
  4. Verify text-only response has no false image detection

Usage:
    python scripts/test_images.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.chatgpt.client import ChatGPTClient
from src.config import Config
from src.log import setup_logging

log = setup_logging("test_images", log_file="test_images.log")


TESTS = [
    {
        "name": "Text-only (no false positive)",
        "prompt": "What is 2 + 2? Answer in one word.",
        "expect_image": False,
        "expect_contains": ["4", "four"],
        "expect_any": True,  # any of expect_contains matches = pass
    },
    {
        "name": "Image generation — simple",
        "prompt": "Generate an image of a cute orange tabby cat sitting on a windowsill with sunlight streaming in.",
        "expect_image": True,
    },
    {
        "name": "Follow-up text after image",
        "prompt": "Now describe the image you just created in 2 sentences.",
        "expect_image": False,
    },
    {
        "name": "Image generation — specific style",
        "prompt": "Generate an image of a futuristic cyberpunk city at night with neon lights and flying cars, digital art style.",
        "expect_image": True,
    },
]


def print_header(text: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {text}")
    print(f"{'=' * 70}")


def validate(test: dict, response) -> tuple[bool, str]:
    """Validate test result. Returns (passed, reason)."""
    if test.get("expect_image"):
        if not response.has_images:
            return False, "Expected image but none detected"
        for img in response.images:
            if not img.local_path:
                return False, f"Image detected but download failed (url: {img.url[:60]})"
            if not os.path.exists(img.local_path):
                return False, f"Image file doesn't exist: {img.local_path}"
            size = os.path.getsize(img.local_path)
            if size < 1000:
                return False, f"Image file too small ({size} bytes): {img.local_path}"
        return True, f"{len(response.images)} image(s) downloaded successfully"

    else:
        if response.has_images:
            return False, "False positive — detected images in text-only response"
        if "expect_contains" in test:
            if test.get("expect_any"):
                # ANY of the keywords matching = pass
                found = any(
                    kw.lower() in response.message.lower()
                    for kw in test["expect_contains"]
                )
                if not found:
                    return False, f"Missing all expected text: {test['expect_contains']}"
            else:
                for kw in test["expect_contains"]:
                    if kw.lower() not in response.message.lower():
                        return False, f"Missing expected text: '{kw}'"
        return True, "Text response, no false image detection"


async def main():
    browser = BrowserManager()
    results = []

    try:
        print_header("Image Generation Test Suite")
        print(f"  Tests: {len(TESTS)}")
        print(f"  Images dir: {Config.IMAGES_DIR}")
        print()

        # Launch browser
        print("  Starting browser...")
        page = await browser.start()
        await browser.navigate(Config.CHATGPT_URL)
        await asyncio.sleep(3)

        if not await browser.is_logged_in():
            print("\n  ❌ Not logged in! Run first_login.py first.")
            return

        print("  ✅ Logged in\n")
        client = ChatGPTClient(page)

        # Start fresh
        print("  Starting new chat...")
        await client.new_chat()

        for i, test in enumerate(TESTS, 1):
            print(f"\n  {'─' * 60}")
            print(f"  Test {i}/{len(TESTS)}: {test['name']}")
            print(f"  Prompt: {test['prompt'][:65]}...")
            expects = "image" if test.get("expect_image") else "text only"
            print(f"  Expects: {expects}")
            print(f"  ⏳ Sending...")

            try:
                response = await client.send_message(test["prompt"])

                passed, reason = validate(test, response)
                status = "✅ PASS" if passed else "❌ FAIL"

                result = {
                    "test": test["name"],
                    "prompt": test["prompt"],
                    "passed": passed,
                    "reason": reason,
                    "response_time_ms": response.response_time_ms,
                    "has_images": response.has_images,
                    "image_count": len(response.images),
                    "response_length": len(response.message),
                    "response_preview": response.message[:150],
                }

                if response.images:
                    result["images"] = [
                        {
                            "alt": img.alt,
                            "local_path": img.local_path,
                            "url": img.url[:100],
                            "prompt_title": img.prompt_title,
                        }
                        for img in response.images
                    ]

                results.append(result)

                print(f"  {status} | {reason}")
                print(f"  Time: {response.response_time_ms}ms | Text: {len(response.message)} chars")

                if response.has_images:
                    for img in response.images:
                        print(f"  🎨 Image: {img.alt or img.prompt_title or 'untitled'}")
                        print(f"     Path:  {img.local_path}")
                        if img.local_path and os.path.exists(img.local_path):
                            size_kb = os.path.getsize(img.local_path) / 1024
                            print(f"     Size:  {size_kb:.1f} KB")
                else:
                    # Show text preview
                    preview = response.message[:100].replace("\n", " ")
                    print(f"  Text: {preview}...")

                log.info(f"Test '{test['name']}': {status} — {reason}")

            except Exception as e:
                results.append({
                    "test": test["name"],
                    "passed": False,
                    "reason": f"Error: {e}",
                })
                print(f"  ❌ ERROR: {e}")
                log.error(f"Test '{test['name']}' failed: {e}", exc_info=True)

        # ── Summary ─────────────────────────────────────────────
        print_header("TEST SUMMARY")

        passed = sum(1 for r in results if r["passed"])
        failed = len(results) - passed

        print(f"  Passed: {passed}/{len(results)}")
        print(f"  Failed: {failed}")
        print()

        for r in results:
            icon = "✅" if r["passed"] else "❌"
            time_ms = r.get("response_time_ms", 0)
            img_info = f", {r.get('image_count', 0)} img" if r.get("has_images") else ""
            print(f"  {icon} {r['test']}: {r['reason']} ({time_ms}ms{img_info})")

        # List downloaded images
        if Config.IMAGES_DIR.exists():
            images = list(Config.IMAGES_DIR.glob("*.*"))
            if images:
                print(f"\n  📁 Downloaded images ({len(images)} files):")
                for img_path in sorted(images, key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
                    size_kb = img_path.stat().st_size / 1024
                    print(f"     {img_path.name} ({size_kb:.1f} KB)")

        if failed == 0:
            print(f"\n  🎉 ALL {len(results)} TESTS PASSED!")
        else:
            print(f"\n  ⚠️  {failed} test(s) failed — check logs.")

        # Save results
        results_file = Config.LOG_DIR / "image_test_results.json"
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n  📝 Results saved to: {results_file}")

        print("\n  Browser still open for inspection.")
        input("  Press ENTER to close > ")

    except KeyboardInterrupt:
        print("\n\n  Cancelled by user.")
    except Exception as e:
        log.error(f"Test failed: {e}", exc_info=True)
        print(f"\n  ❌ Fatal error: {e}")
    finally:
        await browser.close()
        print("  Browser closed.\n")


if __name__ == "__main__":
    asyncio.run(main())
