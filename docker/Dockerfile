# ── Stage 1: Build CatGPT with jlesage/baseimage-gui ────────────
FROM jlesage/baseimage-gui:debian-12-v4

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Application settings for jlesage GUI
RUN set-cont-env APP_NAME "CatGPT"
ENV KEEP_APP_RUNNING=1

# ── System dependencies ─────────────────────────────────────────
# Install Python, Chromium runtime libraries, fonts, and base tools with the
# base image helper.  Installing these explicitly avoids Playwright's generic
# apt bootstrap, which is incompatible with the minimized jlesage user/group
# database.
RUN add-pkg \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
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
    ca-certificates

# ── Python Virtual Environment ──────────────────────────────────
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# ── Application setup ───────────────────────────────────────────
WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Patchright's Chromium into a shared path that is identical during
# the root build and the non-root jlesage application runtime.
RUN patchright install chromium

# Copy application source code and scripts
COPY src/ src/
COPY scripts/ scripts/

# ── Directory setup ─────────────────────────────────────────────
RUN mkdir -p /app/browser_data /app/logs /app/downloads/images /app/downloads/audio

# ── GUI initialization & startup scripts ────────────────────────
COPY docker/50-catgpt-init.sh /etc/cont-init.d/50-catgpt-init.sh
COPY docker/startapp.sh /startapp.sh
RUN chmod +x /etc/cont-init.d/50-catgpt-init.sh /startapp.sh

# ── App Environment Variables ───────────────────────────────────
ENV HEADLESS=false
ENV AUTO_LOGIN_INTERACTIVE=false
ENV BROWSER_DATA_DIR=/app/browser_data
ENV LOG_DIR=/app/logs
ENV API_HOST=0.0.0.0
ENV API_PORT=8000
ENV LOG_LEVEL=DEBUG
ENV VERBOSE=true
ENV PYTHONPATH=/app
ENV DISPLAY_WIDTH=1366
ENV DISPLAY_HEIGHT=768

# ── Ports ───────────────────────────────────────────────────────
# 8000: FastAPI server
# 5800: Web GUI (HTTP, or HTTPS when SECURE_CONNECTION=1)
# 5900: VNC
EXPOSE 8000 5800 5900

# ── Health check ────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || curl -f http://localhost:8000/v1/models || exit 1
