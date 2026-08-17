# Chrome and Playwright Runbook

This file records the manual browser workflow used while testing CatGPT issues.
CatGPT stores login state in `browser_data/`; only one Chrome/Chromium process can
use that profile at a time.

## Open Chrome for Login

From the repo root on Windows PowerShell:

```powershell
$profile = Join-Path (Get-Location) 'browser_data'
New-Item -ItemType Directory -Force -Path $profile | Out-Null
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$args = @("--user-data-dir=$profile", "--no-first-run", "--no-default-browser-check", "https://chatgpt.com")
Start-Process -FilePath $chrome -ArgumentList $args
Write-Output "Opened Chrome with profile: $profile"
```

Sign in, wait until the ChatGPT composer is visible, then close that Chrome
window before running Playwright/Patchright tests. If Chrome stays open,
Playwright will fail with a profile-lock error like:

```text
Opening in existing browser session. This usually means that the profile is already in use.
```

## Launch Playwright with Saved Login

Patchright may not have its bundled browser installed locally. When that happens,
launch installed Chrome explicitly:

```python
from patchright.async_api import async_playwright

pw = await async_playwright().start()
context = await pw.chromium.launch_persistent_context(
    user_data_dir="browser_data",
    executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    headless=False,
    slow_mo=50,
    viewport={"width": 1280, "height": 720},
    args=[
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ],
)
page = context.pages[0] if context.pages else await context.new_page()
await page.goto("https://chatgpt.com", wait_until="domcontentloaded")
```

Always close the context when done:

```python
await context.close()
await pw.stop()
```

## Docker Web GUI Login
 
In Docker, use the browser exposed through the jlesage Web GUI:
 
```text
http://localhost:5800
```
 
The API process is non-interactive in the container, so it will not wait for
terminal input. If login is required, sign in through the Web GUI and then check:
 
```bash
curl http://localhost:8000/status
```

## Testing Response Copy Selection

For issue #42, use a prompt that forces an early code block plus a final
sentinel. The extracted response should include both the code marker and the
final sentinel. If only the code marker is present, CatGPT clicked an inline
code-block copy button instead of the response-level copy button.
