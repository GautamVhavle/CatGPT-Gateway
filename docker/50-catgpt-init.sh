#!/bin/sh
set -e

# ── CatGPT Container Initialization (runs as root before startapp.sh) ──

echo "[catgpt-init] Preparing runtime directories..."
mkdir -p /app/browser_data /app/logs /app/downloads/images /app/downloads/audio

# This script runs after jlesage initializes USER_ID/GROUP_ID and the app user.
# Use the base-image helper so bind mounts and network shares are handled safely.
take-ownership /app/browser_data
take-ownership /app/logs
take-ownership /app/downloads

# Clean up stale Chromium locks from previous crash/restart
rm -f /app/browser_data/SingletonLock \
      /app/browser_data/SingletonSocket \
      /app/browser_data/SingletonCookie
echo "[catgpt-init] Stale Chromium locks cleaned"

# Pre-resolve DNS for Chrome to prevent Docker DNS proxy (127.0.0.11) issues
echo "[catgpt-init] Pre-resolving DNS for Chrome..."
python3 -c "
import os, socket
provider = os.environ.get('PROVIDER', 'chatgpt').lower()
common_domains = [
    'challenges.cloudflare.com',
    'static.cloudflareinsights.com',
]
chatgpt_domains = [
    'chatgpt.com',
    'cdn.oaistatic.com',
    'ab.chatgpt.com',
    'auth.openai.com',
    'auth0.openai.com',
    'openai.com',
    'api.openai.com',
    'platform.openai.com',
]
claude_domains = [
    'claude.ai',
    'api.claude.ai',
    'cdn.claude.ai',
    'anthropic.com',
    'www.anthropic.com',
]
domains = common_domains + (claude_domains if provider == 'claude' else chatgpt_domains)
resolved = []
for d in domains:
    try:
        ip = socket.gethostbyname(d)
        resolved.append(f'{ip} {d}')
        print(f'  {d} -> {ip}')
    except Exception as e:
        print(f'  {d} -> FAILED ({e})')

if resolved:
    try:
        with open('/etc/hosts', 'a') as f:
            f.write('\n# Pre-resolved DNS for Chrome (added by catgpt-init)\n')
            for entry in resolved:
                f.write(entry + '\n')
        print(f'  Added {len(resolved)} entries to /etc/hosts')
    except Exception as e:
        print(f'  WARNING: Could not write to /etc/hosts: {e}')
" || true
echo "[catgpt-init] Initialization complete."
