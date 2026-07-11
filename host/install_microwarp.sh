#!/usr/bin/env bash
# Install MicroWARP SOCKS5 (WARP exit IP) via Docker.
# https://github.com/ccbkkb/MicroWARP
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/grok-auto-register}"
COMPOSE_DIR="${APP_DIR}/deploy/linux/microwarp"
PROXY_URL="${MICROWARP_PROXY:-socks5://127.0.0.1:1080}"

echo "[microwarp] compose dir: $COMPOSE_DIR"
mkdir -p "$COMPOSE_DIR"
cd "$COMPOSE_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "[microwarp] ERROR: docker not found"
  exit 1
fi

# Prefer docker compose v2
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "[microwarp] ERROR: docker compose not available"
  exit 1
fi

# Official warp-cli can conflict with WireGuard / ports — disable if present
if systemctl is-active --quiet warp-svc 2>/dev/null; then
  echo "[microwarp] stopping official warp-svc to avoid conflict..."
  warp-cli --accept-tos disconnect 2>/dev/null || true
  systemctl disable --now warp-svc 2>/dev/null || true
fi

echo "[microwarp] pulling image & starting..."
"${DC[@]}" pull
"${DC[@]}" up -d
sleep 4

echo "[microwarp] container status:"
docker ps --filter name=microwarp --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker stats microwarp --no-stream 2>/dev/null || true

echo "[microwarp] verify exit IP:"
echo -n "  direct: "
curl -4 -sS --max-time 10 https://ifconfig.me || echo fail
echo
echo -n "  warp:   "
curl -4 -sS --max-time 20 -x "socks5h://127.0.0.1:1080" https://ifconfig.me || echo fail
echo
echo "[microwarp] CF trace (via proxy):"
curl -4 -sS --max-time 20 -x "socks5h://127.0.0.1:1080" https://1.1.1.1/cdn-cgi/trace | head -15 || true
echo
echo "[microwarp] done. proxy=${PROXY_URL}"
