#!/usr/bin/env python3
"""
Multi-Turn Test — Validates conversation continuity and new chat functionality.

Test Plan:
  Round 1: Start a NEW CHAT, send 5 follow-up messages in the same thread
  Round 2: Start ANOTHER NEW CHAT, send 5 more follow-up messages

Each message response is verified and logged. The test only confirms
completion after the response has fully streamed.

Usage:
    python scripts/test_multi_turn.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.chatgpt.client import ChatGPTClient
from src.config import Config
from src.log import setup_logging

log = setup_logging("test_multi_turn", log_file="test_multi_turn.log")

# ── Test Messages ───────────────────────────────────────────────

ROUND_1_MESSAGES = [
    "Hi! Let's do a quick test. Please respond with only: 'Round 1, Message 1 received.' Nothing else.",
    "Great. Now respond with only: 'Round 1, Message 2 received.' Nothing else.",
    "Perfect. Now respond with only: 'Round 1, Message 3 received.' Nothing else.",
    "Good. Now respond with only: 'Round 1, Message 4 received.' Nothing else.",
    "Last one. Respond with only: 'Round 1, Message 5 received.' Nothing else.",
]

ROUND_2_MESSAGES = [
    "Hello again! This is a new conversation. Respond with only: 'Round 2, Message 1 received.' Nothing else.",
    "Continue here. Respond with only: 'Round 2, Message 2 received.' Nothing else.",
    "Still going. Respond with only: 'Round 2, Message 3 received.' Nothing else.",
    "Almost done. Respond with only: 'Round 2, Message 4 received.' Nothing else.",
    "Final message. Respond with only: 'Round 2, Message 5 received.' Nothing else.",
]


def print_header(text: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}\n")


def print_result(idx: int, msg: str, response: str, time_ms: int, thread_id: str) -> None:
    status = "✅" if response.strip() else "❌"
    print(f"  {status} [{idx}] ({time_ms}ms) Thread: {thread_id or 'n/a'}")
    print(f"     Sent: {msg[:60]}...")
    print(f"     Got:  {response[:80]}")
    print()


async def run_round(
    client: ChatGPTClient,
    round_name: str,
    messages: list[str],
    results: list[dict],
) -> str:
    """Send a series of messages and collect results. Returns thread_id."""
    print_header(f"{round_name} — {len(messages)} messages")

    thread_id = ""
    for i, msg in enumerate(messages, 1):
        print(f"  ⏳ [{round_name}] Sending message {i}/{len(messages)}...")
        log.info(f"[{round_name}] Sending message {i}: {msg[:60]}")

        try:
            response = await client.send_message(msg)
            thread_id = response.thread_id or thread_id

            result = {
                "round": round_name,
                "message_num": i,
                "sent": msg,
                "response": response.message,
                "response_time_ms": response.response_time_ms,
                "thread_id": thread_id,
                "status": "ok" if response.message.strip() else "empty",
            }
            results.append(result)

            print_result(i, msg, response.message, response.response_time_ms, thread_id)
            log.info(
                f"[{round_name}] Message {i} OK — {response.response_time_ms}ms, "
                f"{len(response.message)} chars: {response.message[:60]}"
            )

        except Exception as e:
            result = {
                "round": round_name,
                "message_num": i,
                "sent": msg,
                "response": "",
                "response_time_ms": 0,
                "thread_id": thread_id,
                "status": f"error: {e}",
            }
            results.append(result)
            print(f"  ❌ [{i}] ERROR: {e}")
            log.error(f"[{round_name}] Message {i} FAILED: {e}", exc_info=True)

    return thread_id


async def main():
    browser = BrowserManager()
    results: list[dict] = []

    try:
        print_header("Multi-Turn Test — ChatGPT Automation")
        print("  This test will:")
        print("  1. Open a NEW chat and send 5 follow-up messages")
        print("  2. Open ANOTHER new chat and send 5 more messages")
        print("  3. Report all results\n")

        # Launch browser
        print("  Starting browser...")
        page = await browser.start()
        await browser.navigate(Config.CHATGPT_URL)
        await asyncio.sleep(3)

        # Verify login
        if not await browser.is_logged_in():
            print("\n  ❌ Not logged in! Run 'python scripts/first_login.py' first.")
            return

        print("  ✅ Logged in\n")
        client = ChatGPTClient(page)

        # ── Round 1: New chat + 5 messages ──────────────────────
        print("  Starting new chat for Round 1...")
        await client.new_chat()

        round1_thread = await run_round(client, "Round 1", ROUND_1_MESSAGES, results)

        print(f"  Round 1 complete — Thread: {round1_thread}")
        print(f"  {'─' * 50}")

        # ── Round 2: Another new chat + 5 messages ──────────────
        print("\n  Starting new chat for Round 2...")
        await client.new_chat()

        round2_thread = await run_round(client, "Round 2", ROUND_2_MESSAGES, results)

        print(f"  Round 2 complete — Thread: {round2_thread}")

        # ── Summary ─────────────────────────────────────────────
        print_header("TEST SUMMARY")

        ok_count = sum(1 for r in results if r["status"] == "ok")
        fail_count = len(results) - ok_count
        total_time = sum(r["response_time_ms"] for r in results)

        print(f"  Total messages:  {len(results)}")
        print(f"  Successful:      {ok_count}")
        print(f"  Failed:          {fail_count}")
        print(f"  Total time:      {total_time}ms ({total_time / 1000:.1f}s)")
        print(f"  Avg response:    {total_time // len(results)}ms")
        print(f"  Round 1 thread:  {round1_thread or 'n/a'}")
        print(f"  Round 2 thread:  {round2_thread or 'n/a'}")
        print(f"  Threads differ:  {'✅ Yes' if round1_thread != round2_thread else '❌ No (same thread!)'}")
        print()

        if fail_count == 0:
            print("  🎉 ALL TESTS PASSED — Ready for Phase 2!")
        else:
            print(f"  ⚠️  {fail_count} message(s) had issues — check logs.")

        # Save results
        results_file = Config.LOG_DIR / "multi_turn_results.json"
        with open(results_file, "w") as f:
            json.dump({
                "summary": {
                    "total": len(results),
                    "ok": ok_count,
                    "failed": fail_count,
                    "total_time_ms": total_time,
                    "round1_thread": round1_thread,
                    "round2_thread": round2_thread,
                },
                "results": results,
            }, f, indent=2, default=str)
        print(f"\n  📝 Results saved to: {results_file}")

        # Keep browser open for inspection
        print("\n  Browser still open for inspection.")
        input("  Press ENTER to close > ")

    except KeyboardInterrupt:
        print("\n\n  Cancelled by user.")
    except Exception as e:
        log.error(f"Test failed: {e}", exc_info=True)
        print(f"\n  ❌ Fatal error: {e}")
        print("  Check logs/test_multi_turn.log for details.")
    finally:
        await browser.close()
        print("  Browser closed.\n")


if __name__ == "__main__":
    asyncio.run(main())
