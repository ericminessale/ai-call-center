#!/bin/sh
# Trigger the backend's daily demo-reset endpoint. Refuses on the
# server side when DEMO_MODE is unset, so this is safe to run on a
# clone-and-own deployment (it'll just no-op).
#
# Auth: HTTP Basic via WEBHOOK_AUTH_USER / WEBHOOK_AUTH_PASSWORD —
# the same shared secret all our internal endpoints use.

set -e

BACKEND_URL="${BACKEND_URL:-http://backend:5000}"
USER="${WEBHOOK_AUTH_USER:-}"
PASS="${WEBHOOK_AUTH_PASSWORD:-}"

# When the backend's webhook_auth is in soft-mode (empty creds), the
# request still works — the endpoint accepts it and logs a warning.
# When in enforce mode, missing creds get a 401, which is fine — the
# next run after operator wires the env vars will succeed.
AUTH_ARGS=""
if [ -n "$USER" ] && [ -n "$PASS" ]; then
    AUTH_ARGS="-u ${USER}:${PASS}"
fi

echo "[demo-reset] $(date -u +%Y-%m-%dT%H:%M:%SZ) firing reset against ${BACKEND_URL}"

# -f: fail on HTTP errors. -s: silent. --max-time 60: full reset
# should take well under a minute; if it hangs longer we want to
# notice and not pile up cron jobs.
RESPONSE=$(curl -fsS --max-time 60 \
    $AUTH_ARGS \
    -X POST \
    -H "Content-Type: application/json" \
    "${BACKEND_URL}/api/internal/demo-reset" \
    || echo "ERROR: curl failed")

echo "[demo-reset] response: ${RESPONSE}"
