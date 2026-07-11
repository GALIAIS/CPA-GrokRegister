#!/usr/bin/env bash
# Build grok-register.so against a local CLIProxyAPI tree.
set -euo pipefail
CPA_DIR="${CPA_DIR:-/root/CLIProxyAPI}"
OUT="${OUT:-$CPA_DIR/plugins/grok-register.so}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$(dirname "$OUT")"
mkdir -p "$CPA_DIR/examples/plugin/grok-register/go"
cp -a "$ROOT/plugin/." "$CPA_DIR/examples/plugin/grok-register/"

cd "$CPA_DIR/examples/plugin/grok-register/go"
if ! grep -q 'replace github.com/router-for-me/CLIProxyAPI/v7' go.mod 2>/dev/null; then
  echo "replace github.com/router-for-me/CLIProxyAPI/v7 => ../../../../" >>go.mod
fi

if command -v go >/dev/null 2>&1; then
  CGO_ENABLED=1 go build -buildmode=c-shared -o "$OUT" .
else
  docker run --rm -v "${CPA_DIR}:/src" -w /src/examples/plugin/grok-register/go golang:1.26 \
    bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gcc >/dev/null && CGO_ENABLED=1 /usr/local/go/bin/go build -buildmode=c-shared -o /src/plugins/grok-register.so .'
fi
ls -la "$OUT"
echo "built: $OUT"
