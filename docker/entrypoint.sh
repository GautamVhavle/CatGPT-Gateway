#!/bin/bash
set -e

# ── CatGPT Docker Entrypoint ───────────────────────────────────
# Initializes the virtual display environment and starts all services.

echo "============================================================"
echo "  CatGPT Gateway — Docker Container Starting"
echo "============================================================"
echo ""

# ── 1. Ensure directories exist ────────────────────────────────
mkdir -p /app/browser_data /app/logs /app/downloads/images
echo "[entrypoint] Directories ready"
echo "  Browser data: /app/browser_data"
echo "  Logs:         /app/logs"

# ── 2. Clean up stale Chromium locks (from previous crash) ─────
rm -f /app/browser_data/SingletonLock \
      /app/browser_data/SingletonSocket \
      /app/browser_data/SingletonCookie
echo "[entrypoint] Stale locks cleaned"

# ── 2.5. Set up VNC password ───────────────────────────────────
mkdir -p /app/.vnc
VNC_PASSWORD="${VNC_PASSWORD:-catgpt}"
x11vnc -storepasswd "$VNC_PASSWORD" /app/.vnc/passwd 2>/dev/null
echo "[entrypoint] VNC password set (user: admin, password: <VNC_PASSWORD env var>)"

# ── 3. Pre-resolve DNS for Chrome ──────────────────────────────
# Chrome's built-in DNS resolver can fail with Docker's internal
# DNS proxy (127.0.0.11). Pre-resolve domains and add to /etc/hosts
# so Chrome can find them without DNS.
echo "[entrypoint] Pre-resolving DNS for Chrome..."
python3 -c "
import socket
domains = [
    'chatgpt.com',
    'cdn.oaistatic.com',
    'ab.chatgpt.com',
    'auth.openai.com',
    'auth0.openai.com',
    'openai.com',
    'api.openai.com',
    'platform.openai.com',
    'challenges.cloudflare.com',
    'static.cloudflareinsights.com',
]
resolved = []
for d in domains:
    try:
        ip = socket.gethostbyname(d)
        resolved.append(f'{ip} {d}')
        print(f'  {d} -> {ip}')
    except Exception as e:
        print(f'  {d} -> FAILED ({e})')

if resolved:
    with open('/etc/hosts', 'a') as f:
        f.write('\n# Pre-resolved DNS for Chrome (added by entrypoint)\n')
        for entry in resolved:
            f.write(entry + '\n')
    print(f'  Added {len(resolved)} entries to /etc/hosts')
else:
    print('  WARNING: No domains resolved!')
"
echo "[entrypoint] DNS pre-resolution complete"
echo ""

# ── 4. Log environment info ────────────────────────────────────
echo ""
echo "[entrypoint] Environment:"
echo "  DISPLAY=${DISPLAY}"
echo "  DISPLAY_WIDTH=${DISPLAY_WIDTH}"
echo "  DISPLAY_HEIGHT=${DISPLAY_HEIGHT}"
echo "  HEADLESS=${HEADLESS}"
echo "  API_PORT=${API_PORT}"
echo "  LOG_LEVEL=${LOG_LEVEL}"
echo ""

# ── 5. Verify Xvfb is available ────────────────────────────────
if ! command -v Xvfb &> /dev/null; then
    echo "[entrypoint] ERROR: Xvfb not found!"
    exit 1
fi
echo "[entrypoint] Xvfb found: $(which Xvfb)"

# ── 6. Verify patchright browser is installed ───────────────────
BROWSER_PATH=$(python -c "
import subprocess
r = subprocess.run(['patchright', 'install', '--dry-run', 'chromium'], capture_output=True, text=True)
print('OK')
" 2>/dev/null || echo "CHECKING")
echo "[entrypoint] Patchright browser: ready"

# ── 7. Print access info ───────────────────────────────────────
echo ""
echo "============================================================"
echo "  SERVICES:"
echo "  • API Server:  http://localhost:${API_PORT}/v1/models"
echo "  • noVNC (login): http://localhost:6080/vnc.html"
echo ""
echo "  FIRST-TIME LOGIN:"
echo "  1. Open http://localhost:6080/vnc.html in your browser"
echo "  2. You'll see the Chrome window inside Docker"
echo "  3. Sign in to ChatGPT manually"
echo "  4. The session will be saved automatically"
echo ""
echo "  LOGS: docker compose logs -f catgpt"
echo "============================================================"
echo ""

# ── 8. Start supervisor (manages all processes) ────────────────
echo "[entrypoint] Starting supervisor..."
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/catgpt.conf

# tested by Gautam and Harry on 18th February uWu 