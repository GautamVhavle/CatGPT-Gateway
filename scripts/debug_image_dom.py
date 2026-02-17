#!/usr/bin/env python3
"""
Debug script — captures DOM structure after ChatGPT generates an image.

Sends an image generation prompt, waits for the user to confirm the image
appeared, then dumps the relevant DOM HTML for analysis.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.config import Config
from src.log import setup_logging

log = setup_logging("debug_images")


async def main():
    browser = BrowserManager()

    try:
        print("Starting browser...")
        page = await browser.start()
        await browser.navigate(Config.CHATGPT_URL)
        await asyncio.sleep(3)

        if not await browser.is_logged_in():
            print("NOT LOGGED IN!")
            return

        print("Logged in. Starting new chat...")
        await page.goto(Config.CHATGPT_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Type the prompt
        prompt = "Generate an image of a cute orange cat."
        print(f"Sending: {prompt}")

        # Find and fill input
        input_el = await page.wait_for_selector("#prompt-textarea", timeout=10000)
        await page.evaluate("el => { el.focus(); el.innerText = arguments[0]; }", prompt)
        await input_el.evaluate("el => el.innerText = '" + prompt.replace("'", "\\'") + "'")
        await asyncio.sleep(0.5)

        # Trigger input event
        await page.evaluate("""
            () => {
                const el = document.querySelector('#prompt-textarea');
                if (el) {
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }
        """)
        await asyncio.sleep(0.5)

        # Click send
        try:
            send_btn = await page.wait_for_selector(
                "button[data-testid='send-button']", timeout=5000
            )
            await send_btn.click()
            print("Sent!")
        except Exception:
            await page.keyboard.press("Enter")
            print("Sent via Enter!")

        # Wait for the image to generate
        print("\n" + "=" * 60)
        print("WAITING — please watch the browser and confirm the image appears.")
        print("Press ENTER once you see the generated image...")
        print("=" * 60)
        input()

        # Now capture the DOM
        print("\nCapturing DOM structure...")

        # 1. All conversation turns
        turns_html = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll(
                    'article, [data-testid*="conversation-turn"], [class*="turn"]'
                );
                const results = [];
                for (const turn of turns) {
                    results.push({
                        tag: turn.tagName,
                        testId: turn.getAttribute('data-testid') || '',
                        classes: turn.className || '',
                        htmlSnippet: turn.innerHTML.substring(0, 2000),
                    });
                }
                return results;
            }
        """)

        # 2. All assistant messages
        assistant_html = await page.evaluate("""
            () => {
                const msgs = document.querySelectorAll(
                    '[data-message-author-role="assistant"]'
                );
                const results = [];
                for (const msg of msgs) {
                    results.push({
                        tag: msg.tagName,
                        classes: msg.className || '',
                        htmlSnippet: msg.innerHTML.substring(0, 3000),
                    });
                }
                return results;
            }
        """)

        # 3. All images on the page
        all_images = await page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img');
                const results = [];
                for (const img of imgs) {
                    results.push({
                        src: img.src || '',
                        alt: img.alt || '',
                        width: img.naturalWidth || img.width || 0,
                        height: img.naturalHeight || img.height || 0,
                        classes: img.className || '',
                        parentTag: img.parentElement?.tagName || '',
                        parentClasses: img.parentElement?.className || '',
                        grandparentTag: img.parentElement?.parentElement?.tagName || '',
                        nearestArticle: (() => {
                            let el = img;
                            for (let i = 0; i < 15; i++) {
                                if (!el.parentElement) return 'none';
                                el = el.parentElement;
                                if (el.tagName === 'ARTICLE') return el.getAttribute('data-testid') || 'article';
                                if (el.getAttribute('data-message-author-role')) return 'msg:' + el.getAttribute('data-message-author-role');
                            }
                            return 'none';
                        })()
                    });
                }
                return results;
            }
        """)

        # 4. All buttons in the last turn
        last_turn_buttons = await page.evaluate("""
            () => {
                // Find the last article/turn
                const articles = document.querySelectorAll('article');
                if (articles.length === 0) return [];
                const last = articles[articles.length - 1];
                const buttons = last.querySelectorAll('button');
                return Array.from(buttons).map(btn => ({
                    text: (btn.innerText || '').trim().substring(0, 100),
                    ariaLabel: btn.getAttribute('aria-label') || '',
                    testId: btn.getAttribute('data-testid') || '',
                    classes: btn.className || '',
                }));
            }
        """)

        # 5. Check for canvas elements
        canvases = await page.evaluate("""
            () => {
                return Array.from(document.querySelectorAll('canvas')).map(c => ({
                    width: c.width,
                    height: c.height,
                    classes: c.className,
                    parentTag: c.parentElement?.tagName,
                }));
            }
        """)

        # 6. All links with download attribute
        download_links = await page.evaluate("""
            () => {
                return Array.from(document.querySelectorAll('a[download], a[aria-label="Download"]')).map(a => ({
                    href: a.href || '',
                    download: a.getAttribute('download') || '',
                    ariaLabel: a.getAttribute('aria-label') || '',
                    text: (a.innerText || '').substring(0, 100),
                }));
            }
        """)

        # Print results
        print("\n" + "=" * 60)
        print("DOM ANALYSIS RESULTS")
        print("=" * 60)

        print(f"\n--- CONVERSATION TURNS ({len(turns_html)}) ---")
        for i, t in enumerate(turns_html):
            print(f"\n  Turn {i+1}: <{t['tag']}> testId='{t['testId']}' classes='{t['classes'][:80]}'")
            print(f"  HTML (first 500 chars): {t['htmlSnippet'][:500]}")

        print(f"\n--- ASSISTANT MESSAGES ({len(assistant_html)}) ---")
        for i, m in enumerate(assistant_html):
            print(f"\n  Msg {i+1}: <{m['tag']}> classes='{m['classes'][:80]}'")
            print(f"  HTML (first 800 chars): {m['htmlSnippet'][:800]}")

        print(f"\n--- ALL IMAGES ({len(all_images)}) ---")
        for i, img in enumerate(all_images):
            print(f"\n  Image {i+1}:")
            print(f"    src: {img['src'][:120]}")
            print(f"    alt: {img['alt'][:100]}")
            print(f"    size: {img['width']}x{img['height']}")
            print(f"    classes: {img['classes'][:80]}")
            print(f"    parent: <{img['parentTag']}> .{img['parentClasses'][:60]}")
            print(f"    grandparent: <{img['grandparentTag']}>")
            print(f"    nearest article: {img['nearestArticle']}")

        print(f"\n--- LAST TURN BUTTONS ({len(last_turn_buttons)}) ---")
        for i, btn in enumerate(last_turn_buttons):
            print(f"  Button {i+1}: text='{btn['text'][:50]}' aria='{btn['ariaLabel']}' testId='{btn['testId']}'")

        print(f"\n--- CANVASES ({len(canvases)}) ---")
        for i, c in enumerate(canvases):
            print(f"  Canvas {i+1}: {c['width']}x{c['height']} parent=<{c['parentTag']}>")

        print(f"\n--- DOWNLOAD LINKS ({len(download_links)}) ---")
        for i, dl in enumerate(download_links):
            print(f"  Link {i+1}: href={dl['href'][:100]} download='{dl['download']}' aria='{dl['ariaLabel']}'")

        # Save to file
        import json
        debug_data = {
            "turns": turns_html,
            "assistant_messages": assistant_html,
            "images": all_images,
            "last_turn_buttons": last_turn_buttons,
            "canvases": canvases,
            "download_links": download_links,
        }
        debug_file = Config.LOG_DIR / "image_dom_debug.json"
        with open(debug_file, "w") as f:
            json.dump(debug_data, f, indent=2)
        print(f"\n📝 Full debug data saved to: {debug_file}")

        print("\nPress ENTER to close browser...")
        input()

    except KeyboardInterrupt:
        print("\nCancelled.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await browser.close()
        print("Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())
