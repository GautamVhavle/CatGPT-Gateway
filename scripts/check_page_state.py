"""Quick diagnostic: check what's actually on the page right now."""
import asyncio, sys
sys.path.insert(0, ".")
from patchright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = [p for p in browser.contexts[0].pages if "chatgpt.com" in p.url][0]
    
    result = await page.evaluate("""
        () => {
            const turns = document.querySelectorAll('section[data-testid^="conversation-turn-"]');
            const info = [];
            for (let i = 0; i < turns.length; i++) {
                const t = turns[i];
                const role = t.getAttribute('data-turn') || 'unknown';
                const text = (t.innerText || '').trim().substring(0, 150);
                const copyBtn = t.querySelector('button[data-testid="copy-turn-action-button"], button[aria-label="Copy"], button[aria-label="Copy message"]');
                const allBtns = t.querySelectorAll('button');
                const btnTexts = Array.from(allBtns).map(b => (b.getAttribute('aria-label') || b.innerText || '').trim().substring(0, 30)).filter(Boolean);
                const stableId = t.getAttribute('data-turn-id') || t.getAttribute('data-testid') || '';
                info.push({
                    index: i,
                    role,
                    stableId,
                    textLen: text.length,
                    textPreview: text.substring(0, 100),
                    hasCopyBtn: !!copyBtn,
                    buttonCount: allBtns.length,
                    buttonLabels: btnTexts.slice(0, 8),
                });
            }
            return info;
        }
    """)
    
    print(f"\n{'='*70}")
    print(f"PAGE STATE: {len(result)} turns")
    print(f"{'='*70}")
    for t in result:
        copy_status = "✓ COPY" if t["hasCopyBtn"] else "✗ no copy"
        print(f"\n  Turn {t['index']} ({t['role']}) [{copy_status}] — {t['textLen']} chars")
        print(f"    ID: {t['stableId']}")
        print(f"    Text: {t['textPreview'][:80]}")
        print(f"    Buttons ({t['buttonCount']}): {t['buttonLabels']}")
    
    await pw.stop()

asyncio.run(main())
