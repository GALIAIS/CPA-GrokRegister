#!/usr/bin/env bash
# One-click installer for CPA-GrokRegister on a Linux host that already runs CLIProxyAPI.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/GALIAIS/CPA-GrokRegister/main/scripts/install.sh | sudo bash
#   # or from a local clone:
#   sudo bash scripts/install.sh
#
# Optional env:
#   APP_DIR=/opt/grok-auto-register
#   CPA_DIR=/root/CLIProxyAPI
#   REGISTER_REPO=https://github.com/GALIAIS/grok-auto-register.git   # worker source (optional)
#   CONTROL_TOKEN=...                                                 # auto-generated if empty
#   SKIP_MICROWARP=0
#   SKIP_WORKER_CLONE=0
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[install] please run as root (sudo)"
  exit 1
fi

PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/grok-auto-register}"
CPA_DIR="${CPA_DIR:-/root/CLIProxyAPI}"
CONTROL_TOKEN="${CONTROL_TOKEN:-}"
SKIP_MICROWARP="${SKIP_MICROWARP:-0}"
# If 1, do not overwrite existing worker files under APP_DIR (except control scripts)
SKIP_WORKER_SYNC="${SKIP_WORKER_SYNC:-0}"

echo "========================================"
echo "  CPA-GrokRegister installer"
echo "========================================"
echo "  PKG_ROOT=$PKG_ROOT"
echo "  APP_DIR =$APP_DIR"
echo "  CPA_DIR =$CPA_DIR"
echo

if [[ -z "$CONTROL_TOKEN" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    CONTROL_TOKEN="$(openssl rand -hex 16)"
  else
    CONTROL_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
  echo "[install] generated CONTROL_TOKEN=${CONTROL_TOKEN}"
fi

# ---------- 1) worker tree (bundled under ./worker) ----------
mkdir -p "$APP_DIR" "$APP_DIR/deploy/linux" "$APP_DIR/register_output/auths" "$APP_DIR/register_output/full"

if [[ -d "$PKG_ROOT/worker" ]]; then
  if [[ "$SKIP_WORKER_SYNC" == "1" && -f "$APP_DIR/grok_register_ttk.py" ]]; then
    echo "[install] SKIP_WORKER_SYNC=1 — keep existing worker at $APP_DIR"
  else
    echo "[install] syncing bundled worker → $APP_DIR"
    # Preserve existing config.json if present
    KEEP_CFG=""
    if [[ -f "$APP_DIR/config.json" ]]; then
      KEEP_CFG="$(mktemp)"
      cp -a "$APP_DIR/config.json" "$KEEP_CFG"
    fi
    # rsync if available, else cp -a
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete \
        --exclude 'config.json' \
        --exclude 'register_output/' \
        --exclude '.venv/' \
        --exclude '__pycache__/' \
        --exclude '.browser_profiles/' \
        "$PKG_ROOT/worker/" "$APP_DIR/"
    else
      # copy tree without wiping outputs
      cp -a "$PKG_ROOT/worker/." "$APP_DIR/"
    fi
    if [[ -n "$KEEP_CFG" && -f "$KEEP_CFG" ]]; then
      cp -a "$KEEP_CFG" "$APP_DIR/config.json"
      rm -f "$KEEP_CFG"
      echo "[install] preserved existing config.json"
    fi
  fi
else
  echo "[install] ERROR: bundled worker/ missing in package"
  exit 1
fi

# always refresh control plane + unit files from package
cp -f "$PKG_ROOT/control/register_control.py" "$APP_DIR/deploy/linux/register_control.py"
cp -f "$PKG_ROOT/control/register-control.service" "$APP_DIR/deploy/linux/register-control.service"
cp -f "$PKG_ROOT/host/grok-register.service" "$APP_DIR/deploy/linux/grok-register.service"
if [[ -f "$PKG_ROOT/host/install_microwarp.sh" ]]; then
  cp -f "$PKG_ROOT/host/install_microwarp.sh" "$APP_DIR/deploy/linux/install_microwarp.sh"
  chmod +x "$APP_DIR/deploy/linux/install_microwarp.sh" || true
fi
if [[ -d "$PKG_ROOT/host/microwarp" ]]; then
  mkdir -p "$APP_DIR/deploy/linux/microwarp"
  cp -a "$PKG_ROOT/host/microwarp/." "$APP_DIR/deploy/linux/microwarp/"
fi

if [[ ! -f "$APP_DIR/config.json" ]]; then
  if [[ -f "$PKG_ROOT/host/config.linux.example.json" ]]; then
    cp -f "$PKG_ROOT/host/config.linux.example.json" "$APP_DIR/config.json"
  elif [[ -f "$APP_DIR/config.example.json" ]]; then
    cp -f "$APP_DIR/config.example.json" "$APP_DIR/config.json"
  elif [[ -f "$APP_DIR/deploy/linux/config.linux.json" ]]; then
    cp -f "$APP_DIR/deploy/linux/config.linux.json" "$APP_DIR/config.json"
  fi
  echo "[install] wrote config.json from example — edit secrets before start"
fi

# ---------- 2) python venv for control (and worker if present) ----------
export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip curl ca-certificates >/dev/null

if [[ ! -d "$APP_DIR/.venv" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
# shellcheck disable=SC1091
source "$APP_DIR/.venv/bin/activate"
python -m pip install -U pip setuptools wheel >/dev/null
if [[ -f "$APP_DIR/requirements.txt" ]]; then
  echo "[install] pip install worker requirements..."
  pip install -r "$APP_DIR/requirements.txt"
  # best-effort browser deps for cloak/playwright
  python -m playwright install chromium 2>/dev/null || true
  python -m playwright install-deps chromium 2>/dev/null || true
else
  pip install curl_cffi requests >/dev/null || true
  echo "[install] WARN: requirements.txt missing"
fi

# ---------- 3) MicroWARP (optional) ----------
if [[ "$SKIP_MICROWARP" != "1" ]]; then
  if command -v docker >/dev/null 2>&1; then
    mkdir -p "$APP_DIR/deploy/linux/microwarp"
    cp -f "$PKG_ROOT/host/microwarp/docker-compose.yml" "$APP_DIR/deploy/linux/microwarp/docker-compose.yml"
    if [[ -f "$PKG_ROOT/host/install_microwarp.sh" ]]; then
      chmod +x "$PKG_ROOT/host/install_microwarp.sh"
      APP_DIR="$APP_DIR" bash "$PKG_ROOT/host/install_microwarp.sh" || echo "[install] MicroWARP install warned (continue)"
    fi
  else
    echo "[install] docker not found — skip MicroWARP"
  fi
fi

# ---------- 4) register-control service ----------
cp -f "$APP_DIR/deploy/linux/register-control.service" /etc/systemd/system/register-control.service
cat >/etc/default/register-control <<EOF
REGISTER_CONTROL_HOST=0.0.0.0
REGISTER_CONTROL_PORT=18927
REGISTER_CONTROL_TOKEN=${CONTROL_TOKEN}
REGISTER_UNIT=grok-register
REGISTER_APP_DIR=${APP_DIR}
REGISTER_SSE_INTERVAL_SEC=1.0
REGISTER_CONTROL_PUBLIC_URL=http://127.0.0.1:18927
EOF
systemctl daemon-reload
systemctl enable --now register-control
sleep 1
curl -sS "http://127.0.0.1:18927/health" || true
echo

# ---------- 5) grok-register worker unit (may fail if worker missing) ----------
if [[ -f "$APP_DIR/grok_register_ttk.py" ]]; then
  cp -f "$APP_DIR/deploy/linux/grok-register.service" /etc/systemd/system/grok-register.service
  systemctl daemon-reload
  systemctl enable grok-register.service || true
  echo "[install] worker unit installed (not auto-started — use panel Start)"
else
  echo "[install] skip grok-register unit (worker script missing)"
fi

# ---------- 6) CPA plugin ----------
if [[ ! -d "$CPA_DIR" ]]; then
  echo "[install] WARN: CPA_DIR not found: $CPA_DIR — skip plugin install"
else
  mkdir -p "$CPA_DIR/plugins" "$CPA_DIR/examples/plugin/grok-register/go"
  cp -a "$PKG_ROOT/plugin/." "$CPA_DIR/examples/plugin/grok-register/"
  # detect gateway from running container
  GW="172.17.0.1"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cli-proxy-api; then
    GW="$(docker exec cli-proxy-api sh -c "ip route 2>/dev/null | awk '/default/{print \$3; exit}'" || true)"
    [[ -z "$GW" ]] && GW="$(docker inspect cli-proxy-api --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' 2>/dev/null || true)"
  fi
  [[ -z "$GW" ]] && GW="172.17.0.1"
  CONTROL_URL="http://${GW}:18927"
  echo "[install] control URL for container: $CONTROL_URL"

  # build .so
  if command -v go >/dev/null 2>&1 && [[ -d "$CPA_DIR/sdk/pluginapi" ]]; then
    echo "[install] building plugin with host go..."
    (
      cd "$CPA_DIR/examples/plugin/grok-register/go"
      # point module replace to CPA root
      if ! grep -q 'replace github.com/router-for-me/CLIProxyAPI/v7' go.mod 2>/dev/null; then
        echo "replace github.com/router-for-me/CLIProxyAPI/v7 => ../../../../" >>go.mod
      fi
      CGO_ENABLED=1 go build -buildmode=c-shared -o "$CPA_DIR/plugins/grok-register.so" .
    )
  else
    echo "[install] building plugin with golang docker image..."
    docker run --rm -v "${CPA_DIR}:/src" -w /src/examples/plugin/grok-register/go golang:1.26 \
      bash -c 'set -e
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gcc >/dev/null
        if ! grep -q "replace github.com/router-for-me/CLIProxyAPI/v7" go.mod; then
          echo "replace github.com/router-for-me/CLIProxyAPI/v7 => ../../../../" >> go.mod
        fi
        CGO_ENABLED=1 /usr/local/go/bin/go build -buildmode=c-shared -o /src/plugins/grok-register.so .
      '
  fi
  ls -la "$CPA_DIR/plugins/grok-register.so"

  # patch config.yaml (inject under plugins.configs)
  CONTROL_URL="$CONTROL_URL" CONTROL_TOKEN="$CONTROL_TOKEN" CPA_DIR="$CPA_DIR" python3 - <<'PY'
from pathlib import Path
import os
cpa = Path(os.environ["CPA_DIR"])
url = os.environ["CONTROL_URL"]
tok = os.environ["CONTROL_TOKEN"]
p = cpa / "config.yaml"
if not p.exists():
    (cpa / "plugins.snippet.yaml").write_text(
        "plugins:\n  enabled: true\n  dir: plugins\n  configs:\n"
        "    grok-register:\n      enabled: true\n      priority: 10\n"
    )
    print("[install] no config.yaml — wrote plugins.snippet.yaml")
    raise SystemExit(0)
t = p.read_text(encoding="utf-8")
block = (
    "    grok-register:\n"
    "      enabled: true\n"
    "      priority: 10\n"
    f'      control_url: "{url}"\n'
    f'      control_token: "{tok}"\n'
)
if "grok-register:" not in t:
    if "  configs:" in t:
        t = t.replace("  configs:", "  configs:\n" + block, 1)
    else:
        t += "\nplugins:\n  enabled: true\n  dir: plugins\n  configs:\n" + block
    t = t.replace("plugins:\n  enabled: false", "plugins:\n  enabled: true", 1)
    p.write_text(t, encoding="utf-8")
    print("[install] config.yaml patched")
else:
    print("[install] grok-register already in config.yaml")
PY

  # compose volume + env
  COMPOSE="$CPA_DIR/docker-compose.yml"
  if [[ -f "$COMPOSE" ]]; then
    python3 - <<'PY'
from pathlib import Path
import os
p = Path(os.environ.get("CPA_DIR", "/root/CLIProxyAPI")) / "docker-compose.yml"
t = p.read_text()
changed = False
if "plugins:/CLIProxyAPI/plugins" not in t:
    n = "      - ${CLI_PROXY_LOG_PATH:-./logs}:/CLIProxyAPI/logs"
    if n in t:
        t = t.replace(n, n + "\n      - ${CLI_PROXY_PLUGIN_PATH:-./plugins}:/CLIProxyAPI/plugins")
        changed = True
        print("[install] compose plugins volume added")
if "GROK_REGISTER_CONTROL_URL" not in t:
    o = "    environment:\n      DEPLOY: ${DEPLOY:-}"
    n = (
        "    environment:\n"
        "      DEPLOY: ${DEPLOY:-}\n"
        "      GROK_REGISTER_CONTROL_URL: ${GROK_REGISTER_CONTROL_URL:-http://172.17.0.1:18927}\n"
        "      GROK_REGISTER_CONTROL_TOKEN: ${GROK_REGISTER_CONTROL_TOKEN:-}"
    )
    if o in t:
        t = t.replace(o, n)
        changed = True
        print("[install] compose env added")
if changed:
    p.write_text(t)
PY
    cat >"$CPA_DIR/.env.grok-register" <<EOF
GROK_REGISTER_CONTROL_URL=${CONTROL_URL}
GROK_REGISTER_CONTROL_TOKEN=${CONTROL_TOKEN}
EOF
    if command -v docker >/dev/null 2>&1; then
      cd "$CPA_DIR"
      set -a
      # shellcheck disable=SC1091
      source .env.grok-register
      set +a
      docker compose up -d || docker restart cli-proxy-api || true
    fi
  fi
fi

echo
echo "========================================"
echo "  Install complete"
echo "========================================"
echo "  Control WebUI:"
echo "    http://<HOST_IP>:18927/?token=${CONTROL_TOKEN}"
echo "  API health:"
echo "    curl -sS http://127.0.0.1:18927/health"
echo "  CPA panel (management key required):"
echo "    /v0/resource/plugins/grok-register/panel"
echo "  Token saved in: /etc/default/register-control"
echo
echo "  Next:"
echo "    1) Edit $APP_DIR/config.json (shiromail / cpa keys / proxy)"
echo "       or open WebUI Settings and save"
echo "    2) Open WebUI or CPA panel → Start unlimited"
echo "========================================"
