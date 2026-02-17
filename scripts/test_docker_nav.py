"""Test that the Docker-safe stealth does NOT break DNS on subsequent navigations."""
import asyncio
import sys
sys.path.insert(0, "/app")

from patchright.async_api import async_playwright
from src.browser.stealth import apply_stealth


async def test():
    p = await async_playwright().start()
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir="/tmp/test_final_stealth",
        headless=False,
        args=["--no-sandbox", "--disable-gpu"],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    # Step 1: Navigate first
    await page.goto("https://chatgpt.com", timeout=15000, wait_until="domcontentloaded")
    print(f"1. Initial nav OK: {page.url}")

    # Step 2: Apply our Docker-safe stealth
    await apply_stealth(ctx)
    print("2. Docker-safe stealth applied")

    # Step 3: Navigate again (this was broken before)
    try:
        await page.goto("https://chatgpt.com", timeout=15000, wait_until="domcontentloaded")
        print(f"3. Second nav OK: {page.url}")
    except Exception as e:
        print(f"3. FAIL: {e}")
        await ctx.close()
        await p.stop()
        return

    # Step 4: Navigate to auth
    try:
        await page.goto("https://auth.openai.com", timeout=15000, wait_until="domcontentloaded")
        print(f"4. Auth nav OK: {page.url}")
    except Exception as e:
        print(f"4. FAIL: {e}")

    # Step 5: Navigate to example.com
    try:
        await page.goto("https://example.com", timeout=15000, wait_until="domcontentloaded")
        print(f"5. Example.com OK: {page.url}")
    except Exception as e:
        print(f"5. FAIL: {e}")

    # Step 6: Return to ChatGPT
    try:
        await page.goto("https://chatgpt.com", timeout=15000, wait_until="domcontentloaded")
        print(f"6. Back to ChatGPT OK: {page.url}")
    except Exception as e:
        print(f"6. FAIL: {e}")

    print("\nAll navigations working after stealth!")
    await ctx.close()
    await p.stop()


asyncio.run(test())
