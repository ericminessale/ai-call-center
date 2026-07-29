#!/bin/sh
# Trigger the hosted demo's scheduled cleanup. Two independent pieces,
# because the demo's state lives in two places:
#
#   * the backend's workspace GC (Phase 3 reaper — the whole-floor wipe
#     is gone), and
#   * the DemoShop MCP gateway's seeded SQLite DB, which sits on a volume
#     mounted only in that container, so the backend can't reach it.
#
# Both refuse on the server side when DEMO_MODE/TENANCY_MODE is unset, so
# this is safe to run on a clone-and-own deployment (it'll just no-op).
#
# Usage: demo-reset.sh [hourly|nightly|shop]
#   hourly  (default) → POST /api/internal/workspace-gc
#                       reaps EXPIRED workspaces only (rows, ws:{id}:*
#                       Redis keys, verify bindings, seats, epoch bump)
#   nightly           → POST /api/internal/demo-reset
#                       same GC + MAX_WORKSPACES cap enforcement +
#                       template-workspace interaction hygiene, THEN
#                       restores the DemoShop DB to its seeded state
#                       (orders/RMAs from every visitor's calls otherwise
#                       accumulate for the life of the volume)
#   shop              → the DemoShop restore only — for wiping the shop
#                       by hand between demos, without touching workspaces
#
# Auth (backend): HTTP Basic via INTERNAL_AUTH_USER / INTERNAL_AUTH_PASSWORD — the
# segregated secret for the private /api/internal/* API — falling back to
# WEBHOOK_AUTH_USER / WEBHOOK_AUTH_PASSWORD when unset. Must mirror the
# backend's require_internal_auth exactly (shared docker-compose env), so if
# the operator rotates to a distinct INTERNAL_AUTH secret this cron picks it
# up too.
#
# Auth (DemoShop): HTTP Basic via SHOP_RESET_USER / SHOP_RESET_PASSWORD,
# falling back to DEMO_MCP_USER / DEMO_MCP_PASSWORD — the gateway's own
# credentials, which that container already holds, so the hosted demo
# needs no extra secret. Target host is SHOP_RESET_URL (default
# http://demo-mcp-gateway:8101); set it to the empty string to skip the
# DemoShop step on a deployment that doesn't run the bundled gateway.

set -e

MODE="${1:-hourly}"
BACKEND_URL="${BACKEND_URL:-http://backend:5000}"
USER="${INTERNAL_AUTH_USER:-${WEBHOOK_AUTH_USER:-}}"
PASS="${INTERNAL_AUTH_PASSWORD:-${WEBHOOK_AUTH_PASSWORD:-}}"

# ${VAR-default} (no colon) on purpose: an explicitly-empty SHOP_RESET_URL
# disables the step, while unset falls back to the compose hostname.
SHOP_RESET_URL="${SHOP_RESET_URL-http://demo-mcp-gateway:8101}"
SHOP_RESET_PATH="/internal/shop-reset"
SHOP_USER="${SHOP_RESET_USER:-${DEMO_MCP_USER:-demo}}"
SHOP_PASS="${SHOP_RESET_PASSWORD:-${DEMO_MCP_PASSWORD:-}}"

log()   { echo "[demo-reset] $*"; }
warn()  { echo "[demo-reset] $*" >&2; }
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# POST a workspace-GC endpoint on the backend.
#
# Both endpoints use @require_internal_auth — ALWAYS enforces,
# independent of the WEBHOOK_AUTH_REQUIRED soft-mode knob. (The
# soft-mode escape hatch only applies to /api/webhooks/* +
# /api/queues/<id>/route, not to internal routes — those hold the
# credential-leak surface the 2026-06-02 SEC-01 audit closed.) If creds
# aren't wired into the cron container's env, every GC run silently
# 401s — we surface that as a non-zero exit so docker logs show the
# cron failure instead of pretending success.
workspace_gc() {
    endpoint="$1"
    if [ -z "$USER" ] || [ -z "$PASS" ]; then
        warn "ERROR: INTERNAL_AUTH_USER/PASSWORD (or the WEBHOOK_AUTH_* fallback) not set — ${endpoint} will 401. Wire creds in docker-compose env."
        return 2
    fi

    log "$(stamp) firing ${MODE} GC against ${BACKEND_URL}${endpoint}"

    # -f: fail on HTTP errors. -s: silent. --max-time 60: a GC pass
    # should take well under a minute; if it hangs longer we want to
    # notice and not pile up cron jobs.
    if response=$(curl -fsS --max-time 60 \
        -u "${USER}:${PASS}" \
        -X POST \
        -H "Content-Type: application/json" \
        "${BACKEND_URL}${endpoint}")
    then
        log "response: ${response}"
    else
        warn "ERROR: POST ${endpoint} failed — workspaces were NOT reaped."
        return 1
    fi
}

# Restore the DemoShop SQLite DB to seed state through the demo-mcp
# container's own listener (shop_admin.py). That listener only binds when
# TENANCY_MODE/DEMO_MODE is true, so on a clone-and-own deployment this
# step fails to connect rather than wiping the operator's shop data — one
# more reason it's reported but never allowed to mask the GC's status.
shop_reset() {
    if [ -z "$SHOP_RESET_URL" ]; then
        log "SHOP_RESET_URL is empty — skipping the DemoShop restore."
        return 0
    fi
    if [ -z "$SHOP_PASS" ]; then
        warn "ERROR: SHOP_RESET_PASSWORD (or the DEMO_MCP_PASSWORD fallback) not set — the DemoShop restore will 401. Wire creds in docker-compose env."
        return 2
    fi

    log "$(stamp) restoring seeded DemoShop data via ${SHOP_RESET_URL}${SHOP_RESET_PATH}"

    if response=$(curl -fsS --max-time 60 \
        -u "${SHOP_USER}:${SHOP_PASS}" \
        -X POST \
        -H "Content-Type: application/json" \
        "${SHOP_RESET_URL}${SHOP_RESET_PATH}")
    then
        log "DemoShop response: ${response}"
    else
        warn "ERROR: DemoShop restore failed — orders/RMAs from today's visitors are still in the shop DB. Is demo-mcp-gateway up with TENANCY_MODE/DEMO_MODE set?"
        return 1
    fi
}

RC=0
case "$MODE" in
    nightly)
        # The workspace GC is the primary job — run it first and keep its
        # exit status, so a DemoShop hiccup can't mask a reaper failure.
        workspace_gc "/api/internal/demo-reset" || RC=$?
        SHOP_RC=0
        shop_reset || SHOP_RC=$?
        if [ "$RC" -eq 0 ]; then
            RC="$SHOP_RC"
        fi
        ;;
    shop)
        shop_reset || RC=$?
        ;;
    *)
        workspace_gc "/api/internal/workspace-gc" || RC=$?
        ;;
esac

exit "$RC"
