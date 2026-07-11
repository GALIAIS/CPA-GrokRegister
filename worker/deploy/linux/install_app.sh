#!/usr/bin/env bash
# Install grok-auto-register runtime on Linux (venv + deps + browser libs).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/grok-auto-register}"
export DEBIAN_FRONTEND=noninteractive

echo "[app] target: $APP_DIR"
cd "$APP_DIR"

apt-get update -y
apt-get install -y \
  python3 python3-pip python3-venv python3-dev \
  build-essential \
  curl ca-certificates git \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
  libx11-6 libx11-xcb1 libxcb1 libxext6 libxshmfence1 fonts-liberation \
  xvfb

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -r requirements.txt

# CloakBrowser / Playwright browsers if needed
python - <<'PY' || true
try:
    import cloakbrowser
    print("[app] cloakbrowser import ok", getattr(cloakbrowser, "__version__", "?"))
except Exception as e:
    print("[app] cloakbrowser import:", e)
try:
    from playwright.sync_api import sync_playwright
    print("[app] playwright import ok")
except Exception as e:
    print("[app] playwright:", e)
PY

# Best-effort browser binary install for cloak / playwright
python -m playwright install chromium 2>/dev/null || true
python -m playwright install-deps chromium 2>/dev/null || true

mkdir -p register_output/auths register_output/full logs
chmod +x deploy/linux/*.sh 2>/dev/null || true

if [[ ! -f config.json ]]; then
  if [[ -f deploy/linux/config.linux.json ]]; then
    cp deploy/linux/config.linux.json config.json
    echo "[app] created config.json from config.linux.json — edit secrets if needed"
  elif [[ -f config.example.json ]]; then
    cp config.example.json config.json
    echo "[app] created config.json from example — MUST fill secrets"
  fi
fi

echo "[app] install complete"
python -c "import sys; print(sys.version); import curl_cffi, requests; print('deps ok')"
