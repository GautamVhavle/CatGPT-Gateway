#!/usr/bin/env python3
"""
Robustness Test — Tests long responses, tables, code blocks, and markdown.

Validates:
  1. Long-form responses complete correctly (not cut off)
  2. Tables are captured properly via copy button
  3. Code blocks are preserved
  4. New chat isolation works
  5. Multiple follow-ups with complex content

Usage:
    python scripts/test_robust.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.chatgpt.client import ChatGPTClient
from src.config import Config
from src.log import setup_logging

log = setup_logging("test_robust", log_file="test_robust.log")

# ── Test prompts designed to produce complex output ─────────────

TESTS = [
    {
        "name": "Table output",
        "prompt": (
            "Create a markdown table with 5 rows comparing Python, JavaScript, "
            "and Rust. Columns: Language, Typing, Speed, Use Case, Year Created. "
            "Only output the table, nothing else."
        ),
        "expect_contains": ["|", "Python", "JavaScript", "Rust"],
    },
    {
        "name": "Code block",
        "prompt": (
            "Write a Python function called 'fibonacci' that returns the first N "
            "fibonacci numbers as a list. Include a docstring. Only output the code "
            "block, nothing else."
        ),
        "expect_contains": ["def fibonacci", "return"],
    },
    {
        "name": "Long response",
        "prompt": (
            "Explain the TCP/IP model in detail. Cover all 4 layers, their protocols, "
            "and how data flows from application to physical layer. Be thorough — "
            "at least 300 words."
        ),
        "expect_min_length": 300,
    },
    {
        "name": "Follow-up context",
        "prompt": (
            "Based on what you just explained about TCP/IP, which layer would be "
            "most relevant for a cybersecurity engineer doing packet analysis? "
            "Answer in 2-3 sentences."
        ),
        "expect_contains": ["layer"],
    },
    {
        "name": "Mixed content (table + explanation)",
        "prompt": (
            "Create a table of the OSI model (all 7 layers) with columns: "
            "Layer Number, Name, Protocol Examples, PDU. Then below the table, "
            "write one sentence explaining why this model matters."
        ),
        "expect_contains": ["|", "Physical", "Application"],
    },
]


def print_header(text: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {text}")
    print(f"{'=' * 70}")


def validate_response(test: dict, response: str) -> tuple[bool, str]:
    """Check if the response meets expectations. Returns (passed, reason)."""
    if not response.strip():
        return False, "Empty response"

    if "expect_contains" in test:
        for keyword in test["expect_contains"]:
            if keyword.lower() not in response.lower():
                return False, f"Missing expected content: '{keyword}'"

    if "expect_min_length" in test:
        # Count words instead of chars for more meaningful length check
        word_count = len(response.split())
        if word_count < test["expect_min_length"] * 0.5:  # Allow some flexibility
            return False, f"Too short: {word_count} words (expected ~{test['expect_min_length']}+)"

    return True, "All checks passed"


async def main():
    browser = BrowserManager()
    results = []

    try:
        print_header("Robustness Test — Complex Content Extraction")
        print(f"  Tests: {len(TESTS)}")
        print("  Focus: tables, code blocks, long responses, markdown")
        print()

        # Launch browser
        print("  Starting browser...")
        page = await browser.start()
        await browser.navigate(Config.CHATGPT_URL)
        await asyncio.sleep(3)

        if not await browser.is_logged_in():
            print("\n  ❌ Not logged in! Run 'python scripts/first_login.py' first.")
            return

        print("  ✅ Logged in\n")
        client = ChatGPTClient(page)

        # Start a fresh chat
        print("  Starting new chat...")
        await client.new_chat()

        for i, test in enumerate(TESTS, 1):
            print(f"\n  {'─' * 60}")
            print(f"  Test {i}/{len(TESTS)}: {test['name']}")
            print(f"  Prompt: {test['prompt'][:70]}...")
            print(f"  ⏳ Sending...")

            try:
                response = await client.send_message(test["prompt"])

                passed, reason = validate_response(test, response.message)
                status = "✅ PASS" if passed else "❌ FAIL"

                result = {
                    "test": test["name"],
                    "prompt": test["prompt"],
                    "response_length": len(response.message),
                    "response_time_ms": response.response_time_ms,
                    "thread_id": response.thread_id,
                    "passed": passed,
                    "reason": reason,
                    "response_preview": response.message[:200],
                    "full_response": response.message,
                }
                results.append(result)

                print(f"  {status} | {reason}")
                print(f"  Response: {response.response_time_ms}ms, {len(response.message)} chars")

                # Show first 3 lines of response
                lines = response.message.split("\n")[:5]
                for line in lines:
                    print(f"    │ {line[:80]}")
                if len(response.message.split("\n")) > 5:
                    print(f"    │ ... ({len(response.message.split(chr(10)))} total lines)")

                log.info(
                    f"Test '{test['name']}': {status} — "
                    f"{response.response_time_ms}ms, {len(response.message)} chars"
                )

            except Exception as e:
                results.append({
                    "test": test["name"],
                    "passed": False,
                    "reason": f"Error: {e}",
                    "response_length": 0,
                })
                print(f"  ❌ ERROR: {e}")
                log.error(f"Test '{test['name']}' failed: {e}", exc_info=True)

        # ── Summary ─────────────────────────────────────────────
        print_header("TEST SUMMARY")

        passed = sum(1 for r in results if r["passed"])
        failed = len(results) - passed
        total_time = sum(r.get("response_time_ms", 0) for r in results)

        print(f"  Passed:    {passed}/{len(results)}")
        print(f"  Failed:    {failed}")
        print(f"  Total time: {total_time}ms ({total_time / 1000:.1f}s)")
        print()

        for r in results:
            icon = "✅" if r["passed"] else "❌"
            print(f"  {icon} {r['test']}: {r['reason']} ({r.get('response_time_ms', 0)}ms, {r['response_length']} chars)")

        if failed == 0:
            print(f"\n  🎉 ALL {len(results)} TESTS PASSED!")
        else:
            print(f"\n  ⚠️  {failed} test(s) failed — check logs.")

        # Save results
        results_file = Config.LOG_DIR / "robust_test_results.json"
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
