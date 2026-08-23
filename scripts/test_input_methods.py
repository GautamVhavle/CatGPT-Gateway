"""
Standalone test: which input method properly triggers React state in ChatGPT?

This script connects to the running browser, sends message 1 normally,
then tests different input methods for message 2 to find which one
causes ChatGPT's backend API call to fire.

Run AFTER the server is started (browser is open and logged in).
Usage: .venv/bin/python scripts/test_input_methods.py
"""

import asyncio
import sys
import time

# Add project root to path
sys.path.insert(0, ".")

from patchright.async_api import async_playwright


INPUT_SELECTOR = "#prompt-textarea"
SEND_BUTTON = "button[data-testid='send-button']"


async def connect_to_browser():
    """Connect to the already-running browser via CDP."""
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    except Exception:
        print("ERROR: Can't connect to browser. Make sure server is running.")
        print("The browser must be launched with --remote-debugging-port=9222")
        await pw.stop()
        return None, None, None
    
    contexts = browser.contexts
    if not contexts:
        print("ERROR: No browser contexts found")
        return None, None, None
    
    pages = contexts[0].pages
    chatgpt_page = None
    for p in pages:
        if "chatgpt.com" in p.url:
            chatgpt_page = p
            break
    
    if not chatgpt_page:
        print(f"ERROR: No ChatGPT page found. Pages: {[p.url for p in pages]}")
        return None, None, None
    
    print(f"Connected to: {chatgpt_page.url}")
    return pw, browser, chatgpt_page


async def check_textarea_state(page) -> dict:
    """Check the current state of the textarea and send button."""
    return await page.evaluate("""
        () => {
            const ta = document.querySelector('#prompt-textarea');
            const sendBtn = document.querySelector("button[data-testid='send-button']");
            
            return {
                textareaExists: !!ta,
                textareaText: ta ? ta.innerText.trim() : null,
                textareaHTML: ta ? ta.innerHTML.substring(0, 200) : null,
                sendBtnExists: !!sendBtn,
                sendBtnDisabled: sendBtn ? sendBtn.disabled : null,
                sendBtnAriaDisabled: sendBtn ? sendBtn.getAttribute('aria-disabled') : null,
                sendBtnClassName: sendBtn ? sendBtn.className.substring(0, 100) : null,
            };
        }
    """)


async def wait_for_network_call(page, timeout=15):
    """Wait for a backend-api/conversation POST and return True if seen."""
    seen = {"called": False}
    
    def on_request(req):
        if "backend-api/conversation" in req.url and req.method == "POST":
            seen["called"] = True
            print(f"  ✓ NETWORK: {req.method} {req.url[:80]}")
    
    page.on("request", on_request)
    
    start = time.time()
    while not seen["called"] and (time.time() - start) < timeout:
        await asyncio.sleep(0.5)
    
    page.remove_listener("request", on_request)
    return seen["called"]


async def count_turns(page) -> int:
    return await page.evaluate(
        "document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
    )


async def method_keyboard_type(page, text):
    """Method 1: keyboard.type() — sends keyDown/keyPress/keyUp per char."""
    el = page.locator(INPUT_SELECTOR).first
    await el.click()
    await asyncio.sleep(0.1)
    # Select all and delete
    mod = "Meta" if sys.platform == "darwin" else "Control"
    await page.keyboard.press(f"{mod}+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.1)
    await page.keyboard.type(text, delay=5)


async def method_clipboard_paste(page, text):
    """Method 2: Set clipboard and paste via Cmd+V."""
    el = page.locator(INPUT_SELECTOR).first
    await el.click()
    await asyncio.sleep(0.1)
    mod = "Meta" if sys.platform == "darwin" else "Control"
    await page.keyboard.press(f"{mod}+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.1)
    
    # Set clipboard via page.evaluate and paste
    await page.evaluate(f"navigator.clipboard.writeText({repr(text)})")
    await asyncio.sleep(0.1)
    await page.keyboard.press(f"{mod}+v")


async def method_fill(page, text):
    """Method 3: Playwright's fill() — uses CDP Input.insertText."""
    el = page.locator(INPUT_SELECTOR).first
    await el.fill(text)


async def method_dispatch_input_event(page, text):
    """Method 4: Set innerHTML + dispatch input event."""
    await page.evaluate("""
        (text) => {
            const ta = document.querySelector('#prompt-textarea');
            if (!ta) return;
            
            // Focus
            ta.focus();
            
            // Clear
            ta.innerHTML = '';
            
            // Create a paragraph with the text (ChatGPT uses <p> inside contenteditable)
            const p = document.createElement('p');
            p.textContent = text;
            ta.appendChild(p);
            
            // Dispatch events that React listens for
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            ta.dispatchEvent(new Event('change', { bubbles: true }));
            
            // Also try the newer InputEvent
            ta.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: text,
            }));
        }
    """, text)


async def method_exec_command(page, text):
    """Method 5: document.execCommand('insertText') — fires proper input events."""
    el = page.locator(INPUT_SELECTOR).first
    await el.click()
    await asyncio.sleep(0.1)
    mod = "Meta" if sys.platform == "darwin" else "Control"
    await page.keyboard.press(f"{mod}+a")
    await asyncio.sleep(0.05)
    
    await page.evaluate("""
        (text) => {
            document.execCommand('insertText', false, text);
        }
    """, text)


async def method_react_native_setter(page, text):
    """Method 6: Use React's internal value setter to update state."""
    await page.evaluate("""
        (text) => {
            const ta = document.querySelector('#prompt-textarea');
            if (!ta) return;
            ta.focus();
            
            // Clear existing content
            ta.innerHTML = '<p>' + text + '</p>';
            
            // Find React fiber and trigger onChange
            const key = Object.keys(ta).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
            if (key) {
                let fiber = ta[key];
                // Walk up to find the component with onChange
                while (fiber) {
                    if (fiber.memoizedProps && fiber.memoizedProps.onChange) {
                        fiber.memoizedProps.onChange({ target: ta });
                        break;
                    }
                    if (fiber.pendingProps && fiber.pendingProps.onChange) {
                        fiber.pendingProps.onChange({ target: ta });
                        break;
                    }
                    fiber = fiber.return;
                }
            }
            
            // Also dispatch standard events
            ta.dispatchEvent(new Event('input', { bubbles: true }));
        }
    """, text)


async def test_method(page, name, method_fn, text):
    """Test a single input method: type text, check state, click send, check network."""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"{'='*60}")
    
    # Check pre-state
    pre_turns = await count_turns(page)
    print(f"  Turns before: {pre_turns}")
    
    # Apply input method
    try:
        await method_fn(page, text)
    except Exception as e:
        print(f"  ✗ Input method FAILED: {e}")
        return False
    
    await asyncio.sleep(0.5)
    
    # Check textarea state after input
    state = await check_textarea_state(page)
    print(f"  Textarea text: {repr(state.get('textareaText', '')[:80])}")
    print(f"  Send button exists: {state.get('sendBtnExists')}")
    print(f"  Send button disabled: {state.get('sendBtnDisabled')}")
    print(f"  Send button aria-disabled: {state.get('sendBtnAriaDisabled')}")
    
    if not state.get("sendBtnExists"):
        print("  ✗ No send button found — input didn't activate it")
        return False
    
    if state.get("sendBtnDisabled"):
        print("  ✗ Send button is DISABLED — React didn't register the text")
        return False
    
    print(f"  → Send button is ENABLED — React registered the input!")
    
    # Click send and watch for network
    print(f"  Clicking send...")
    try:
        send_btn = page.locator(SEND_BUTTON).first
        await send_btn.click()
    except Exception as e:
        print(f"  ✗ Click send failed: {e}")
        return False
    
    # Wait for network call
    print(f"  Waiting for backend API call...")
    got_network = await wait_for_network_call(page, timeout=10)
    
    if got_network:
        print(f"  ✓ SUCCESS — Backend API call detected!")
        # Wait for response
        await asyncio.sleep(8)
        post_turns = await count_turns(page)
        print(f"  Turns after: {post_turns}")
        return True
    else:
        print(f"  ✗ FAILED — No backend API call after 10s")
        post_turns = await count_turns(page)
        print(f"  Turns after: {post_turns}")
        return False


async def main():
    pw, browser, page = await connect_to_browser()
    if not page:
        return
    
    try:
        # First, send message 1 to get into a multi-turn state
        turns = await count_turns(page)
        print(f"\nCurrent turns on page: {turns}")
        
        if turns == 0:
            print("\n--- Sending initial message to establish conversation ---")
            await method_keyboard_type(page, "Say just the word 'hello'")
            await asyncio.sleep(0.5)
            state = await check_textarea_state(page)
            print(f"Send button state: disabled={state.get('sendBtnDisabled')}")
            
            send_btn = page.locator(SEND_BUTTON).first
            await send_btn.click()
            print("Sent. Waiting for response...")
            
            # Wait for assistant response
            for _ in range(30):
                await asyncio.sleep(1)
                turns = await count_turns(page)
                if turns >= 2:  # user + assistant
                    break
            
            print(f"Turns after first message: {turns}")
            await asyncio.sleep(3)  # cooldown
        
        print("\n" + "="*60)
        print("NOW TESTING INPUT METHODS FOR THE SECOND MESSAGE")
        print("="*60)
        
        # Test each method one at a time
        methods = [
            ("keyboard.type()", method_keyboard_type),
            ("clipboard paste (Cmd+V)", method_clipboard_paste),
            ("fill()", method_fill),
            ("execCommand('insertText')", method_exec_command),
            ("dispatch input event", method_dispatch_input_event),
            ("React native setter", method_react_native_setter),
        ]
        
        # Just test what the textarea state looks like after each method
        # WITHOUT clicking send (so we can test multiple methods)
        print("\n--- Testing which methods activate the send button ---\n")
        
        for name, method_fn in methods:
            try:
                # Clear textarea first
                el = page.locator(INPUT_SELECTOR).first
                await el.click()
                await asyncio.sleep(0.1)
                mod = "Meta" if sys.platform == "darwin" else "Control"
                await page.keyboard.press(f"{mod}+a")
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.3)
                
                # Apply method
                await method_fn(page, f"Test from {name}")
                await asyncio.sleep(0.5)
                
                # Check state
                state = await check_textarea_state(page)
                text = state.get("textareaText", "")
                disabled = state.get("sendBtnDisabled")
                exists = state.get("sendBtnExists")
                
                status = "✓ ENABLED" if (exists and not disabled) else "✗ DISABLED/MISSING"
                print(f"  {status} | {name}")
                print(f"         textarea: {repr(text[:60])}")
                if not exists:
                    print(f"         (send button not found)")
                
            except Exception as e:
                print(f"  ✗ ERROR  | {name}: {e}")
        
        # Now do the actual send test with the FIRST method that works
        print("\n--- Finding first method that activates send button ---")
        
        for name, method_fn in methods:
            try:
                el = page.locator(INPUT_SELECTOR).first
                await el.click()
                await asyncio.sleep(0.1)
                mod = "Meta" if sys.platform == "darwin" else "Control"
                await page.keyboard.press(f"{mod}+a")
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.3)
                
                await method_fn(page, "What is 2+2? Just the number.")
                await asyncio.sleep(0.5)
                
                state = await check_textarea_state(page)
                if state.get("sendBtnExists") and not state.get("sendBtnDisabled"):
                    print(f"\n  → Using {name} for the actual send test")
                    result = await test_method(page, name, method_fn, "What is 2+2? Just the number.")
                    if result:
                        print(f"\n{'='*60}")
                        print(f"WINNER: {name}")
                        print(f"{'='*60}")
                    break
            except Exception:
                continue
    
    finally:
        # Don't close — the server still needs the browser
        if pw:
            await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
