"""
Diagnose WHY ChatGPT's backend isn't responding on 2nd message.
Check for hidden errors, rate limits, broken connections.
Then test in a completely fresh chat.
"""
import asyncio, sys, time
sys.path.insert(0, ".")
from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = [p for p in browser.contexts[0].pages if "chatgpt.com" in p.url][0]
    
    # 1. Check for any error state on the page
    print("=" * 60)
    print("STEP 1: Check for errors/overlays on current page")
    print("=" * 60)
    
    errors = await page.evaluate("""
        () => {
            const info = {};
            
            // Check for error messages anywhere
            const allText = document.body.innerText;
            const errorPatterns = [
                'Something went wrong', 'rate limit', 'too many', 
                'try again', 'error', 'couldn\\'t', 'failed',
                'network error', 'unable to', 'capacity'
            ];
            info.errorPatterns = errorPatterns.filter(p => 
                allText.toLowerCase().includes(p.toLowerCase())
            );
            
            // Check for dialogs/modals
            info.dialogs = [];
            document.querySelectorAll('[role="dialog"], [role="alertdialog"], dialog').forEach(d => {
                info.dialogs.push((d.innerText || '').trim().substring(0, 200));
            });
            
            // Check for retry/regenerate buttons
            info.retryButtons = [];
            document.querySelectorAll('button').forEach(btn => {
                const text = (btn.innerText || '').trim().toLowerCase();
                if (text.includes('retry') || text.includes('regenerate') || 
                    text.includes('try again') || text.includes('resend')) {
                    info.retryButtons.push(text);
                }
            });
            
            // Check the empty assistant turn (turn 3) for any hidden elements
            const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
            const lastTurn = turns[turns.length - 1];
            if (lastTurn) {
                info.lastTurnHTML = lastTurn.innerHTML.substring(0, 500);
                info.lastTurnClasses = lastTurn.className;
                info.lastTurnChildren = Array.from(lastTurn.children).map(c => ({
                    tag: c.tagName,
                    className: c.className.substring(0, 50),
                    text: (c.innerText || '').trim().substring(0, 100),
                }));
            }
            
            // Check for any toast/notification elements
            info.toasts = [];
            document.querySelectorAll('[class*="toast"], [class*="notification"], [class*="snackbar"], [class*="alert"]').forEach(el => {
                const text = (el.innerText || '').trim();
                if (text) info.toasts.push(text.substring(0, 200));
            });
            
            // Check network status
            info.isOnline = navigator.onLine;
            
            // Check if there's a stop/cancel button visible (indicating generation)
            const stopBtn = document.querySelector('button[aria-label="Stop generating"], button[data-testid="stop-button"]');
            info.hasStopButton = !!stopBtn;
            
            return info;
        }
    """)
    
    print(f"  Online: {errors.get('isOnline')}")
    print(f"  Error patterns found: {errors.get('errorPatterns', [])}")
    print(f"  Dialogs: {errors.get('dialogs', [])}")
    print(f"  Retry buttons: {errors.get('retryButtons', [])}")
    print(f"  Toasts: {errors.get('toasts', [])}")
    print(f"  Stop button visible: {errors.get('hasStopButton')}")
    print(f"  Last turn classes: {errors.get('lastTurnClasses', '')}")
    print(f"  Last turn children:")
    for child in errors.get('lastTurnChildren', []):
        print(f"    <{child['tag']}> class={child['className']} text={child['text'][:60]}")
    print(f"  Last turn HTML (first 400 chars):")
    html = errors.get('lastTurnHTML', '')
    for line in html[:400].split('\n')[:10]:
        print(f"    {line[:100]}")
    
    # 2. Try to navigate to a fresh chat using the new-chat button
    print(f"\n{'='*60}")
    print("STEP 2: Navigate to fresh chat via button click")
    print("="*60)
    
    # Find and click new chat button
    clicked = await page.evaluate("""
        () => {
            const selectors = [
                "a[data-testid='create-new-chat-button']",
                "a[href='/']",
                "nav a[href='/']",
            ];
            for (const sel of selectors) {
                const btn = document.querySelector(sel);
                if (btn) {
                    btn.click();
                    return sel;
                }
            }
            return null;
        }
    """)
    print(f"  Clicked: {clicked}")
    
    if not clicked:
        print("  ERROR: Could not find new chat button!")
        await pw.stop()
        return
    
    # Wait for navigation
    await asyncio.sleep(3)
    
    # Verify fresh chat
    turn_count = await page.evaluate(
        "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
    )
    print(f"  Turns after clicking new chat: {turn_count}")
    print(f"  URL: {page.url}")
    
    if turn_count != 0:
        print("  WARNING: Not a fresh chat!")
        await pw.stop()
        return
    
    # 3. Wait a moment, then send a message in the fresh chat
    print(f"\n{'='*60}")
    print("STEP 3: Send message in fresh chat")
    print("="*60)
    
    await asyncio.sleep(2)
    
    # Type message
    el = page.locator("#prompt-textarea").first
    await el.click()
    await asyncio.sleep(0.2)
    await page.keyboard.type("What is 5+5? Just the number.", delay=10)
    await asyncio.sleep(0.5)
    
    # Check send button
    state = await page.evaluate("""
        () => {
            const sendBtn = document.querySelector("button[data-testid='send-button']");
            return {
                exists: !!sendBtn,
                disabled: sendBtn ? sendBtn.disabled : null,
                text: sendBtn ? sendBtn.innerText : null,
            };
        }
    """)
    print(f"  Send button: exists={state['exists']}, disabled={state['disabled']}")
    
    if not state['exists'] or state['disabled']:
        print("  ERROR: Send button not ready!")
        await pw.stop()
        return
    
    # Click send
    send_btn = page.locator("button[data-testid='send-button']").first
    await send_btn.click()
    print(f"  Send clicked at {time.strftime('%H:%M:%S')}")
    
    # Wait for response — poll for up to 30 seconds
    print(f"  Waiting for response...")
    for i in range(60):
        await asyncio.sleep(0.5)
        result = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                const last = turns[turns.length - 1];
                if (!last) return { turnCount: 0 };
                
                const role = last.getAttribute('data-turn');
                const text = (last.innerText || '').trim();
                const copyBtn = last.querySelector('button[data-testid="copy-turn-action-button"], button[aria-label="Copy response"]');
                
                return {
                    turnCount: turns.length,
                    lastRole: role,
                    lastTextLen: text.length,
                    lastText: text.substring(0, 100),
                    hasCopy: !!copyBtn,
                };
            }
        """)
        
        if result['turnCount'] >= 2 and result.get('lastRole') == 'assistant':
            if result.get('hasCopy') or result.get('lastTextLen', 0) > 0:
                print(f"  ✓ Response at {time.strftime('%H:%M:%S')} ({(i+1)*0.5:.0f}s)")
                print(f"    Turns: {result['turnCount']}")
                print(f"    Text ({result['lastTextLen']} chars): {result.get('lastText', '')[:60]}")
                print(f"    Copy button: {result.get('hasCopy')}")
                break
        
        if i % 10 == 9:
            print(f"    ...waiting ({(i+1)*0.5:.0f}s, turns={result['turnCount']}, text={result.get('lastTextLen', 0)} chars)")
    else:
        print(f"  ✗ No response after 30s")
        print(f"    Final state: {result}")
    
    # 4. Now send a SECOND message in this fresh chat
    print(f"\n{'='*60}")
    print("STEP 4: Send SECOND message (the critical test)")
    print("="*60)
    
    await asyncio.sleep(5)  # 5-second cooldown
    
    turn_count_before = await page.evaluate(
        "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
    )
    print(f"  Turns before: {turn_count_before}")
    
    # Type second message
    el = page.locator("#prompt-textarea").first
    await el.click()
    await asyncio.sleep(0.2)
    await page.keyboard.type("What is 3+3? Just the number.", delay=10)
    await asyncio.sleep(0.5)
    
    # Verify text is in textarea
    ta_text = await page.evaluate(
        "document.querySelector('#prompt-textarea')?.innerText?.trim() || ''"
    )
    print(f"  Textarea text: {repr(ta_text)}")
    
    # Check send button
    state2 = await page.evaluate("""
        () => {
            const sendBtn = document.querySelector("button[data-testid='send-button']");
            return {
                exists: !!sendBtn,
                disabled: sendBtn ? sendBtn.disabled : null,
            };
        }
    """)
    print(f"  Send button: exists={state2['exists']}, disabled={state2['disabled']}")
    
    # Click send
    send_btn2 = page.locator("button[data-testid='send-button']").first
    await send_btn2.click()
    print(f"  Send clicked at {time.strftime('%H:%M:%S')}")
    
    # Wait for response
    print(f"  Waiting for response...")
    for i in range(60):
        await asyncio.sleep(0.5)
        result2 = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                const last = turns[turns.length - 1];
                if (!last) return { turnCount: 0 };
                
                const role = last.getAttribute('data-turn');
                const text = (last.innerText || '').trim();
                const copyBtn = last.querySelector('button[data-testid="copy-turn-action-button"], button[aria-label="Copy response"]');
                
                return {
                    turnCount: turns.length,
                    lastRole: role,
                    lastTextLen: text.length,
                    lastText: text.substring(0, 100),
                    hasCopy: !!copyBtn,
                };
            }
        """)
        
        if result2['turnCount'] > turn_count_before and result2.get('lastRole') == 'assistant':
            if result2.get('hasCopy') or result2.get('lastTextLen', 0) > 0:
                print(f"  ✓ Response at {time.strftime('%H:%M:%S')} ({(i+1)*0.5:.0f}s)")
                print(f"    Turns: {result2['turnCount']}")
                print(f"    Text ({result2['lastTextLen']} chars): {result2.get('lastText', '')[:60]}")
                print(f"    Copy button: {result2.get('hasCopy')}")
                break
        
        if i % 10 == 9:
            print(f"    ...waiting ({(i+1)*0.5:.0f}s, turns={result2['turnCount']}, text={result2.get('lastTextLen', 0)} chars)")
    else:
        print(f"  ✗ No response after 30s")
        # Check final page state
        final = await page.evaluate("""
            () => {
                const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
                return Array.from(turns).map((t, i) => ({
                    index: i,
                    role: t.getAttribute('data-turn'),
                    textLen: (t.innerText || '').trim().length,
                    text: (t.innerText || '').trim().substring(0, 60),
                    hasCopy: !!t.querySelector('button[data-testid="copy-turn-action-button"]'),
                    btnCount: t.querySelectorAll('button').length,
                }));
            }
        """)
        print(f"  Final page state:")
        for t in final:
            print(f"    Turn {t['index']} ({t['role']}): {t['textLen']} chars, copy={t['hasCopy']}, btns={t['btnCount']}")
            print(f"      Text: {t['text'][:60]}")
    
    await pw.stop()

asyncio.run(main())
