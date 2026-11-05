#!/bin/sh
# Container entrypoint (Render, Docker, docker compose).
# One public port ($PORT) serves the app. The fictional TPA portals and the demo HMS receiver run beside it on
# localhost; the browser reaches their pages through the app (/demo/phone, /demo/hms).
set -e
PORT="${PORT:-8000}"
export PLAYWRIGHT_HEADLESS=true

# Fictional TPA portals, unless PORTAL_BASE_URL points somewhere else (docker compose runs them as their own service).
if [ -z "$PORTAL_BASE_URL" ] || [ "$PORTAL_BASE_URL" = "http://127.0.0.1:8100" ]; then
  export PORTAL_BASE_URL=http://127.0.0.1:8100
  python -m uvicorn mock_portals.server:app --host 127.0.0.1 --port 8100 --log-level warning &
fi

# Demo HMS receiver (Node.js): shows signed claim events arriving. Skipped if a real HMS_WEBHOOK_URL is configured.
if [ "${ENABLE_HMS_DEMO:-true}" = "true" ] && [ -z "$HMS_WEBHOOK_URL" ] && command -v node >/dev/null 2>&1; then
  if [ -z "$HMS_WEBHOOK_SECRET" ]; then
    HMS_WEBHOOK_SECRET="$(python -c 'import secrets; print(secrets.token_hex(16))')"
  fi
  export HMS_WEBHOOK_SECRET HMS_WEBHOOK_URL=http://127.0.0.1:8300/claimpilot HMS_DEMO_URL=http://127.0.0.1:8300
  (cd integrations/hms-node && HOST=127.0.0.1 PORT=8300 exec node webhook-receiver.js) &
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips '*'
