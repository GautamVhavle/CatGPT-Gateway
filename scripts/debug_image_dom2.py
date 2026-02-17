#!/usr/bin/env python3
"""
Debug script — captures DOM structure after ChatGPT generates an image.
Non-interactive version: waits 90 seconds for image generation, then dumps DOM.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.manager import BrowserManager
from src.browser.human import human_type
from src.config import Config
from src.log import setup_logging

log = setup_logging("debug_images")
OUTPUT_FILE = Config.LOG_DIR / "image_dom_debug.json"


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

        prompt = "Generate an image of a cute orange cat."
        print(f"Sending: {prompt}")

        await human_type(page, "#prompt-textarea", prompt)
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

        # Wait for image generation (poll for image appearance)
        print("Waiting for image to appear (max 120s)...")
        found_img = False
        for i in range(120):
            await asyncio.sleep(1)
            # Check for images
            img_count = await page.evaluate("""
                () => {
                    const imgs = document.querySelectorAll('img');
                    let count = 0;
                    for (const img of imgs) {
                        const src = img.src || '';
                        const w = img.naturalWidth || img.width || 0;
                        if (w > 100 && (
                            src.includes('oaidalleapiprodscus') ||
                            src.includes('openai') ||
                            src.includes('dalle') ||
                            (src.startsWith('https://') && w > 200)
                        )) {
                            count++;
                        }
                    }
                    return count;
                }
            """)
            if img_count > 0:
                print(f"  Found {img_count} generated image(s) at {i+1}s!")
                found_img = True
                await asyncio.sleep(3)  # Let DOM settle
                break

            # Also check for progressive indicators
            indicators = await page.evaluate("""
                () => {
                    const body = document.body.innerText;
                    const hasCreating = body.includes('Creating') || body.includes('Generating');
                    const hasCanvas = document.querySelectorAll('canvas').length;

                    // Check for any large img tags (they might not match URL patterns)
                    const allImgs = document.querySelectorAll('img');
                    let largeImgs = 0;
                    for (const img of allImgs) {
                        const w = img.naturalWidth || img.width || 0;
                        if (w > 200) largeImgs++;
                    }

                    return { hasCreating, hasCanvas, largeImgs };
                }
            """)
            if i % 10 == 0:
                print(f"  [{i}s] creating={indicators.get('hasCreating')}, canvas={indicators.get('hasCanvas')}, largeImgs={indicators.get('largeImgs')}")

        if not found_img:
            print("No image detected via initial scan. Capturing DOM anyway...")

        # Capture the full DOM analysis
        print("\nCapturing DOM...")

        # 1. All conversation turns
        turns_html = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll('article');
                return Array.from(turns).map((turn, i) => ({
                    index: i,
                    tag: turn.tagName,
                    testId: turn.getAttribute('data-testid') || '',
                    role: turn.getAttribute('data-message-author-role') || '',
                    html: turn.innerHTML.substring(0, 5000),
                    text: (turn.innerText || '').substring(0, 500),
                }));
            }
        """)

        # 2. All assistant messages
        assistant_html = await page.evaluate("""
            () => {
                const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
                return Array.from(msgs).map((msg, i) => ({
                    index: i,
                    tag: msg.tagName,
                    classes: msg.className || '',
                    html: msg.innerHTML.substring(0, 5000),
                    text: (msg.innerText || '').substring(0, 500),
                }));
            }
        """)

        # 3. ALL images with full ancestry
        all_images = await page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img');
                return Array.from(imgs).map((img, i) => {
                    // Build ancestry chain
                    const ancestry = [];
                    let el = img;
                    for (let j = 0; j < 10; j++) {
                        if (!el.parentElement) break;
                        el = el.parentElement;
                        ancestry.push({
                            tag: el.tagName,
                            id: el.id || '',
                            classes: (el.className || '').substring(0, 80),
                            role: el.getAttribute('data-message-author-role') || '',
                            testId: el.getAttribute('data-testid') || '',
                        });
                    }
                    return {
                        index: i,
                        src: img.src || '',
                        alt: img.alt || '',
                        naturalWidth: img.naturalWidth || 0,
                        naturalHeight: img.naturalHeight || 0,
                        width: img.width || 0,
                        height: img.height || 0,
                        classes: img.className || '',
                        ancestry: ancestry,
                    };
                });
            }
        """)

        # 4. Buttons in last turn
        last_turn_buttons = await page.evaluate("""
            () => {
                const articles = document.querySelectorAll('article');
                if (articles.length === 0) return [];
                const last = articles[articles.length - 1];
                return Array.from(last.querySelectorAll('button')).map((btn, i) => ({
                    index: i,
                    text: (btn.innerText || '').substring(0, 100),
                    ariaLabel: btn.getAttribute('aria-label') || '',
                    testId: btn.getAttribute('data-testid') || '',
                }));
            }
        """)

        # 5. Canvas elements
        canvases = await page.evaluate("""
            () => Array.from(document.querySelectorAll('canvas')).map(c => ({
                width: c.width, height: c.height,
                classes: c.className,
                parentTag: c.parentElement?.tagName,
            }))
        """)

        # 6. Download links
        download_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[download], a[aria-label*="ownload"]'))
                .map(a => ({
                    href: (a.href || '').substring(0, 200),
                    download: a.getAttribute('download') || '',
                    ariaLabel: a.getAttribute('aria-label') || '',
                }))
        """)

        # 7. All iframes (DALL-E might use iframes)
        iframes = await page.evaluate("""
            () => Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: f.src || '',
                classes: f.className || '',
                width: f.width, height: f.height,
            }))
        """)

        # 8. Page URL
        page_url = page.url

        # Build debug data
        debug_data = {
            "page_url": page_url,
            "turns": turns_html,
            "assistant_messages": assistant_html,
            "all_images": all_images,
            "last_turn_buttons": last_turn_buttons,
            "canvases": canvases,
            "download_links": download_links,
            "iframes": iframes,
        }

        # Save
        Config.ensure_dirs()
        with open(OUTPUT_FILE, "w") as f:
            json.dump(debug_data, f, indent=2)

        # Print summary
        print(f"\n{'=' * 60}")
        print(f"TURNS: {len(turns_html)}")
        for t in turns_html:
            print(f"  Turn {t['index']}: testId={t['testId']} text={t['text'][:80]}...")

        print(f"\nASSISTANT MESSAGES: {len(assistant_html)}")
        for m in assistant_html:
            print(f"  Msg {m['index']}: text={m['text'][:80]}...")

        print(f"\nIMAGES: {len(all_images)}")
        for img in all_images:
            is_generated = (
                'openai' in img['src'] or
                'oaidalleapiprodscus' in img['src'] or
                'dalle' in img['src'] or
                img['naturalWidth'] > 200
            )
            marker = " ★ GENERATED" if is_generated else ""
            print(f"  Image {img['index']}: {img['naturalWidth']}x{img['naturalHeight']} src={img['src'][:80]}{marker}")
            if img['alt']:
                print(f"    alt: {img['alt'][:100]}")
            anc = img.get('ancestry', [])
            if anc:
                chain = " > ".join(f"<{a['tag']}>{' role=' + a['role'] if a['role'] else ''}" for a in anc[:5])
                print(f"    ancestry: {chain}")

        print(f"\nBUTTONS (last turn): {len(last_turn_buttons)}")
        for btn in last_turn_buttons:
            print(f"  {btn['text'][:40]} | aria={btn['ariaLabel']} | testId={btn['testId']}")

        print(f"\nCANVASES: {len(canvases)}")
        print(f"DOWNLOAD LINKS: {len(download_links)}")
        for dl in download_links:
            print(f"  href={dl['href'][:80]} download={dl['download']}")
        print(f"IFRAMES: {len(iframes)}")

        print(f"\n📁 Full data saved to: {OUTPUT_FILE}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await browser.close()
        print("Browser closed.")


if __name__ == "__main__":
    asyncio.run(main())
