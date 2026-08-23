"""
Test with the ORIGINAL insert_text approach.
Navigate to fresh chat, send 2 messages, check if both get responses.
"""
import asyncio, sys, time
sys.path.insert(0, ".")
from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = [p for p in browser.contexts[0].pages if "chatgpt.com" in p.url][0]
    
    # Navigate to fresh chat
    print("Navigating to fresh chat...")
    await page.evaluate("""
        () => {
            const btn = document.querySelector("a[data-testid='create-new-chat-button']");
            if (btn) btn.click();
        }
    """)
    await asyncio.sleep(3)
    
    turns = await page.evaluate(
        "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
    )
    print(f"Fresh chat: {turns} turns, URL: {page.url}")
    
    async def send_and_wait(msg, msg_num, delay_before=0):
        if delay_before > 0:
            print(f"\n  Cooldown: {delay_before}s...")
            await asyncio.sleep(delay_before)
        
        print(f"\n--- Message {msg_num}: '{msg}' ---")
        
        # Click textarea and use insert_text (the ORIGINAL method)
        el = page.locator("#prompt-textarea").first
        await el.click()
        await asyncio.sleep(0.2)
        await page.keyboard.insert_text(msg)
        await asyncio.sleep(0.3)
        
        # Verify text and send button
        state = await page.evaluate("""
            () => {
                const ta = document.querySelector('#prompt-textarea');
                const btn = document.querySelector("button[data-testid='send-button']");
                return {
                    text: ta ? ta.innerText.trim() : '',
                    btnExists: !!btn,
                    btnDisabled: btn ? btn.disabled : null,
                };
            }
        """)
        print(f"  Text: {repr(state['text'][:50])}")
        print(f"  Send: exists={state['btnExists']}, disabled={state['btnDisabled']}")
        
        # Click send
        send = page.locator("button[data-testid='send-button']").first
        await send.click()
        t0 = time.time()
        print(f"  Sent at {time.strftime('%H:%M:%S')}")
        
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
                        hasCopy: !!last.querySelector('button[data-testid="copy-turn-action-button"], button[aria-label="Copy response"]'),
                    };
                }
            """)
            
            if r['count'] >= msg_num * 2 and r.get('role') == 'assistant' and r.get('textLen', 0) > 0:
                elapsed = time.time() - t0
                print(f"  ✓ Response in {elapsed:.1f}s: '{r['text'][:50]}' (copy={r['hasCopy']})")
                return True
            
            if i % 10 == 9:
                print(f"    ...waiting ({(i+1)*0.5:.0f}s, turns={r['count']}, text={r.get('textLen',0)} chars)")
        
        print(f"  ✗ No response after 30s")
        return False
    
    # Message 1
    ok1 = await send_and_wait("What is 1+1? Just the number.", 1)
    if not ok1:
        print("\nMessage 1 failed — something fundamentally broken")
        await pw.stop()
        return
    
    # Message 2 with various cooldowns
    ok2 = await send_and_wait("What is 2+2? Just the number.", 2, delay_before=10)
    
    if ok2:
        print(f"\n{'='*60}")
        print("SUCCESS: Both messages got responses!")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print("FAILED: Second message got no response (same as before)")
        print("This confirms the issue is NOT the input method.")
        print("ChatGPT's backend refuses to generate for the 2nd message.")
        print(f"{'='*60}")
    
    await pw.stop()

asyncio.run(main())
