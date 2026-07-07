# ── Stage 1: Build ──────────────────────────────────────────────
FROM python:3.9-slim AS base

ENV DEBIAN_FRONTEND=noninteractive

# ── System dependencies ─────────────────────────────────────────
# Xvfb: virtual framebuffer for headed Chromium in containers
# x11vnc + noVNC + websockify: browser-based VNC access
# chromium: browser used by SeleniumBase CDP / Stealthy Playwright Mode
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    supervisor \
    chromium \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-dejavu-core \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# ── Application setup ───────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY scripts/ scripts/
COPY .env.example .env

RUN mkdir -p /app/browser_data /app/logs /app/downloads/images

COPY docker/supervisord.conf /etc/supervisor/conf.d/catgpt.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Environment ─────────────────────────────────────────────────
ENV DISPLAY=:99
ENV DISPLAY_WIDTH=1280
ENV DISPLAY_HEIGHT=720
ENV DISPLAY_DEPTH=24
ENV CHROME_BIN=/usr/bin/chromium
ENV SELENIUMBASE_USE_CHROMIUM=true
ENV HEADLESS=false
ENV BROWSER_DATA_DIR=/app/browser_data
ENV LOG_DIR=/app/logs
ENV API_HOST=0.0.0.0
ENV API_PORT=8000
ENV LOG_LEVEL=DEBUG
ENV VERBOSE=true

EXPOSE 8000 6080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/v1/models || exit 1

ENTRYPOINT ["/entrypoint.sh"]
