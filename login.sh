#!/usr/bin/env bash
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
source .venv/bin/activate
export USE_XVFB=false
python scripts/first_login.py
