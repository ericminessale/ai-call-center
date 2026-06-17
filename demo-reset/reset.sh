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

# /api/internal/demo-reset uses @require_internal_auth — ALWAYS enforces,
# independent of the WEBHOOK_AUTH_REQUIRED soft-mode knob. (The
# soft-mode escape hatch only applies to /api/webhooks/* +
# /api/queues/<id>/route, not to internal routes — those hold the
# credential-leak surface the 2026-06-02 SEC-01 audit closed.) If creds
# aren't wired into the cron container's env, every nightly reset
# silently 401s — we surface that as a non-zero exit so docker logs
# show the cron failure instead of pretending success.
AUTH_ARGS=""
if [ -n "$USER" ] && [ -n "$PASS" ]; then
    AUTH_ARGS="-u ${USER}:${PASS}"
else
    echo "[demo-reset] ERROR: WEBHOOK_AUTH_USER / WEBHOOK_AUTH_PASSWORD not set — /api/internal/demo-reset will 401. Wire creds in docker-compose env." >&2
    exit 2
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
