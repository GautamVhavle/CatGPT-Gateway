#!/usr/bin/env bash
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
source .venv/bin/activate

# Auto-detect Linux with Xvfb for silent, zero-focus-stealing execution
if [[ "$(uname)" == "Linux" ]] && command -v xvfb-run >/dev/null 2>&1 && [[ "${USE_XVFB:-true}" != "false" ]]; then
    echo "🛡️  Starting CatGPT Gateway in isolated virtual display (Xvfb) for zero focus interruption..."
    echo "🌐 Live browser preview available at: http://127.0.0.1:8000/preview"
    exec xvfb-run -a --server-args="-screen 0 1920x1080x24" python -m src.api.server
else
    exec python -m src.api.server
fi
