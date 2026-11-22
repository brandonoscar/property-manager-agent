#!/usr/bin/env bash
# Boot the demo frontend, run the UI-fuzz Playwright sweep, tear down.
#
# Keyless: VITE_DEMO_MODE=true renders from in-memory fixtures — no backend,
# no Buildium, no LLM key. The sweep injects edge-case rows via ?fuzz=<seed>.
# The EXIT trap guarantees the dev server is killed even if the sweep fails.
#
# Env passthrough (see fuzz/run.mjs): FUZZ_SEEDS, FUZZ_BASE_SEED, FUZZ_CHROMIUM.
set -u

cd "$(dirname "$0")/.." || exit 2 # -> frontend/

# Ensure deps (fresh clone / CI). Playwright is a devDependency; the browser
# binary is expected pre-installed (PLAYWRIGHT_BROWSERS_PATH) or fetched by a
# prior `playwright install` — run.mjs falls back to the default channel.
[ -x node_modules/.bin/vite ] || npm install --no-audit --no-fund

PORT="${FUZZ_PORT:-5173}"
VITE_DEMO_MODE=true node_modules/.bin/vite --port "$PORT" --host 0.0.0.0 \
  > /tmp/ui-fuzz-vite.log 2>&1 &
VPID=$!
trap 'kill "$VPID" 2>/dev/null || true' EXIT

# Wait for the dev server to answer (up to ~40s).
up=0
for _ in $(seq 1 40); do
  if curl -sf -o /dev/null "http://localhost:$PORT/"; then up=1; break; fi
  sleep 1
done
if [ "$up" -ne 1 ]; then
  echo "[ui-fuzz] RESULT=FAIL — dev server never came up (see /tmp/ui-fuzz-vite.log)"
  exit 1
fi

FUZZ_BASE_URL="http://localhost:$PORT" node fuzz/run.mjs
exit $?
