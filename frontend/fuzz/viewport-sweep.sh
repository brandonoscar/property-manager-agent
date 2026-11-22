#!/usr/bin/env bash
# Boot the demo frontend, measure layout + touch targets in a real browser,
# tear down. Same shape as fuzz/sweep.sh — keyless, no backend, no LLM key.
#
# Env passthrough (see fuzz/viewports.mjs): VIEWPORT_CHROMIUM.
set -u

cd "$(dirname "$0")/.." || exit 2 # -> frontend/

[ -x node_modules/.bin/vite ] || npm install --no-audit --no-fund

PORT="${VIEWPORT_PORT:-5174}"
VITE_DEMO_MODE=true node_modules/.bin/vite --port "$PORT" --host 0.0.0.0 \
  > /tmp/viewport-vite.log 2>&1 &
VPID=$!
trap 'kill "$VPID" 2>/dev/null || true' EXIT

up=0
for _ in $(seq 1 40); do
  if curl -sf -o /dev/null "http://localhost:$PORT/"; then up=1; break; fi
  sleep 1
done
if [ "$up" -ne 1 ]; then
  echo "[viewports] RESULT=FAIL — dev server never came up (see /tmp/viewport-vite.log)"
  exit 1
fi

VIEWPORT_BASE_URL="http://localhost:$PORT" node fuzz/viewports.mjs
exit $?
