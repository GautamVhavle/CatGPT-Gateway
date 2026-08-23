"""
Capture JavaScript console errors while sending messages.
This will reveal WHY ChatGPT's frontend fails to send the 2nd message.
"""
import asyncio, sys, time
sys.path.insert(0, ".")
from patchright.async_api import async_playwright

# Collect console messages
console_msgs = []

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = [p for p in browser.contexts[0].pages if "chatgpt.com" in p.url][0]
    
    # Listen for console errors and warnings
    def on_console(msg):
        if msg.type in ("error", "warning"):
            console_msgs.append({
                "time": time.strftime("%H:%M:%S"),
                "type": msg.type,
                "text": msg.text[:300],
            })
    
    # Listen for page errors (uncaught exceptions)
    def on_pageerror(error):
        console_msgs.append({
            "time": time.strftime("%H:%M:%S"),
            "type": "PAGE_ERROR",
            "text": str(error)[:500],
        })
    
    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    
    # Navigate to fresh chat
    print("Navigating to fresh chat...")
    console_msgs.clear()
    await page.evaluate("""
        () => { document.querySelector("a[data-testid='create-new-chat-button']")?.click(); }
    """)
    await asyncio.sleep(3)
    
    turns = await page.evaluate(
        "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
    )
    print(f"Fresh chat: {turns} turns")
    
    if console_msgs:
        print(f"\nConsole messages during navigation:")
        for m in console_msgs:
            print(f"  [{m['time']}] {m['type']}: {m['text'][:120]}")
    
    # --- Message 1 ---
    print(f"\n{'='*60}")
    print("MESSAGE 1")
    print(f"{'='*60}")
    console_msgs.clear()
    
    el = page.locator("#prompt-textarea").first
    await el.click()
    await asyncio.sleep(0.2)
    await page.keyboard.insert_text("What is 1+1? Reply with just the number.")
    await asyncio.sleep(0.3)
    
    # Try ENTER instead of clicking send button
    print("  Sending via Enter key...")
    await page.keyboard.press("Enter")
    t0 = time.time()
    
    # Wait for response
    for i in range(40):
        await asyncio.sleep(0.5)
        r = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                const last = turns[turns.length - 1];
                if (!last) return { count: 0 };
                return {
                    count: turns.length,
                    role: last.getAttribute('data-turn'),
                    text: (last.innerText || '').trim().substring(0, 100),
                    textLen: (last.innerText || '').trim().length,
                    hasCopy: !!last.querySelector('button[data-testid="copy-turn-action-button"]'),
                };
            }
        """)
        if r.get('hasCopy') and r.get('role') == 'assistant' and r.get('textLen', 0) > 0:
            print(f"  ✓ Response in {time.time()-t0:.1f}s: '{r['text'][:50]}'")
            break
        if i % 10 == 9:
            print(f"    ...waiting ({(i+1)*0.5:.0f}s, turns={r['count']}, text={r.get('textLen',0)} chars)")
    else:
        print(f"  ✗ No response after 20s")
        if console_msgs:
            print(f"\n  Console errors during message 1:")
            for m in console_msgs:
                print(f"    [{m['time']}] {m['type']}: {m['text'][:150]}")
        await pw.stop()
        return
    
    if console_msgs:
        print(f"\n  Console messages during message 1:")
        for m in console_msgs:
            print(f"    [{m['time']}] {m['type']}: {m['text'][:150]}")
    
    # --- Message 2 (with 10s cooldown) ---
    print(f"\n{'='*60}")
    print("MESSAGE 2 (10s cooldown)")
    print(f"{'='*60}")
    
    await asyncio.sleep(10)
    console_msgs.clear()
    
    # Check current state
    pre = await page.evaluate("""
        () => {
            const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
            const ta = document.querySelector('#prompt-textarea');
            return {
                turnCount: turns.length,
                textareaExists: !!ta,
                textareaFocusable: ta ? ta.getAttribute('contenteditable') : null,
                url: window.location.href,
            };
        }
    """)
    print(f"  Pre-state: {pre}")
    
    el2 = page.locator("#prompt-textarea").first
    await el2.click()
    await asyncio.sleep(0.2)
    await page.keyboard.insert_text("What is 2+2? Reply with just the number.")
    await asyncio.sleep(0.3)
    
    # Verify text
    ta_text = await page.evaluate(
        "document.querySelector('#prompt-textarea')?.innerText?.trim() || ''"
    )
    print(f"  Textarea text: {repr(ta_text[:60])}")
    
    # Check send button state  
    btn_state = await page.evaluate("""
        () => {
            const btn = document.querySelector("button[data-testid='send-button']");
            if (!btn) return { exists: false };
            return { 
                exists: true, 
                disabled: btn.disabled,
                ariaDisabled: btn.getAttribute('aria-disabled'),
                visible: btn.offsetParent !== null,
                rect: btn.getBoundingClientRect(),
            };
        }
    """)
    print(f"  Send button: {btn_state}")
    
    if console_msgs:
        print(f"\n  Console errors BEFORE send:")
        for m in console_msgs:
            print(f"    [{m['time']}] {m['type']}: {m['text'][:150]}")
    
    console_msgs.clear()
    
    # Send via Enter key
    print("  Sending via Enter key...")
    await page.keyboard.press("Enter")
    t0 = time.time()
    
    # Check console immediately
    await asyncio.sleep(1)
    if console_msgs:
        print(f"\n  Console errors IMMEDIATELY after send:")
        for m in console_msgs:
            print(f"    [{m['time']}] {m['type']}: {m['text'][:200]}")
    
    # Wait for response
    for i in range(60):
        await asyncio.sleep(0.5)
        r = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                const last = turns[turns.length - 1];
                if (!last) return { count: 0 };
                return {
                    count: turns.length,
                    role: last.getAttribute('data-turn'),
                    text: (last.innerText || '').trim().substring(0, 100),
                    textLen: (last.innerText || '').trim().length,
                    hasCopy: !!last.querySelector('button[data-testid="copy-turn-action-button"]'),
                };
            }
        """)
        if r.get('hasCopy') and r.get('role') == 'assistant' and r.get('textLen', 0) > 0:
            print(f"  ✓ Response in {time.time()-t0:.1f}s: '{r['text'][:50]}'")
            break
        if i % 10 == 9:
            print(f"    ...waiting ({(i+1)*0.5:.0f}s, turns={r['count']}, text={r.get('textLen',0)} chars)")
            if console_msgs:
                print(f"    Console errors so far:")
                for m in console_msgs[-5:]:
                    print(f"      [{m['time']}] {m['type']}: {m['text'][:150]}")
    else:
        print(f"  ✗ No response after 30s")
    
    print(f"\n{'='*60}")
    print(f"ALL CONSOLE MESSAGES ({len(console_msgs)} total)")
    print(f"{'='*60}")
    for m in console_msgs:
        print(f"  [{m['time']}] {m['type']}: {m['text'][:200]}")
    
    await pw.stop()

asyncio.run(main())
