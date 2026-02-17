#!/usr/bin/env python3
"""
Phase 1 Test Script — Validates the full pipeline.

Steps:
1. Launch browser with persistent session
2. Check login status
3. Start DOM observer + network recorder
4. Optionally start a new chat
5. Send a test message
6. Wait for and capture the response
7. Log everything and print results

Usage:
    python scripts/test_phase1.py
    python scripts/test_phase1.py --message "What is 2+2?"
    python scripts/test_phase1.py --new-chat
"""

import argparse
import asyncio
import json
import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.chatgpt.client import ChatGPTClient
from src.dom_observer import DOMObserver
from src.network_recorder import NetworkRecorder
from src.config import Config
from src.log import setup_logging

log = setup_logging("test_phase1", log_file="test_phase1.log")

DEFAULT_MESSAGE = "Hello! Please respond with exactly: 'Phase 1 test successful.' Nothing else."


async def main(message: str, new_chat: bool, observe: bool):
    browser = BrowserManager()

    try:
        print("\n" + "=" * 60)
        print("  Phase 1 Test — ChatGPT Browser Automation")
        print("=" * 60)

        # 1. Launch browser
        print("\n  [1/7] Launching browser...")
        page = await browser.start()

        # 2. Navigate to ChatGPT
        print("  [2/7] Navigating to ChatGPT...")
        await browser.navigate(Config.CHATGPT_URL)

        # Give page time to fully load
        await asyncio.sleep(3)

        # 3. Check login
        print("  [3/7] Checking login status...")
        logged_in = await browser.is_logged_in()
        if not logged_in:
            print("\n  ❌ Not logged in! Run 'python scripts/first_login.py' first.")
            log.error("Not logged in — aborting test")
            return

        print("  ✅ Logged in")
        log.info("Login confirmed")

        # 4. Start observers (for Phase 1 observation)
        dom_obs = DOMObserver(page)
        net_rec = NetworkRecorder(page)

        if observe:
            print("  [4/7] Starting observers...")
            await dom_obs.start()
            net_rec.start()
            log.info("Observers started")
        else:
            print("  [4/7] Observers skipped (use --observe to enable)")

        # 5. Optionally start new chat
        client = ChatGPTClient(page)

        if new_chat:
            print("  [5/7] Starting new chat...")
            await client.new_chat()
            log.info("New chat started")
        else:
            print("  [5/7] Using current chat (use --new-chat to start fresh)")

        # 6. Send test message
        print(f"  [6/7] Sending message: {message[:60]}...")
        log.info(f"Sending test message: {message}")

        start_time = time.time()
        response = await client.send_message(message)
        elapsed = time.time() - start_time

        # 7. Results
        print("\n" + "-" * 60)
        print("  RESULTS")
        print("-" * 60)
        print(f"\n  Thread ID: {response.thread_id or '(not in URL yet)'}")
        print(f"  Response time: {response.response_time_ms}ms ({elapsed:.1f}s)")
        print(f"  Response length: {len(response.message)} chars")
        print(f"\n  Response:")
        print(f"  {'─' * 50}")
        # Indent response for readability
        for line in response.message.split("\n"):
            print(f"  {line}")
        print(f"  {'─' * 50}")

        log.info(f"Test complete — {response.response_time_ms}ms, {len(response.message)} chars")
        log.info(f"Response: {response.message}")

        # Log network activity
        if observe:
            net_rec.stop()
            await dom_obs.stop()
            captured = net_rec.get_captured()
            log.info(f"Network requests captured: {len(captured)}")
            for req in captured:
                log.debug(f"  {req['method']} {req['url'][:100]}")

            # Save observations to file
            obs_file = Config.LOG_DIR / "phase1_observations.json"
            observations = {
                "test_message": message,
                "response": response.message,
                "response_time_ms": response.response_time_ms,
                "thread_id": response.thread_id,
                "thread_url": await client.get_current_thread_url(),
                "network_requests": captured,
                "selectors_used": {
                    "chat_input": "see logs",
                    "send_button": "see logs",
                    "assistant_response": "see logs",
                },
            }
            with open(obs_file, "w") as f:
                json.dump(observations, f, indent=2, default=str)
            print(f"\n  📝 Observations saved to: {obs_file}")

        # Keep browser open for manual inspection
        print("\n  Browser still open for inspection.")
        input("  Press ENTER to close > ")

    except KeyboardInterrupt:
        print("\n\n  Cancelled by user.")
    except Exception as e:
        log.error(f"Test failed: {e}", exc_info=True)
        print(f"\n  ❌ Error: {e}")
        print("  Check logs for details.")
    finally:
        await browser.close()
        print("  Browser closed.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 Test — ChatGPT Automation")
    parser.add_argument(
        "--message", "-m",
        default=DEFAULT_MESSAGE,
        help="Message to send (default: test prompt)",
    )
    parser.add_argument(
        "--new-chat", "-n",
        action="store_true",
        help="Start a new chat before sending",
    )
    parser.add_argument(
        "--observe", "-o",
        action="store_true",
        help="Enable DOM observer + network recorder",
    )
    args = parser.parse_args()

    asyncio.run(main(args.message, args.new_chat, args.observe))
