#!/bin/sh
# Backend container entrypoint.
#
# Runs the one-time boot sequence BEFORE gunicorn forks its workers, so
# migrations and the admin seed happen exactly once — not once per worker.
#
#   1. Wait for Redis (the app assumes it at import time in a few places).
#   2. flask db upgrade — apply all Alembic migrations to head. This is
#      DEPLOY-H1: on a fresh volume init.sql bootstraps the base schema and
#      stamps a mid-chain revision, but nothing used to run the ~16 revisions
#      after it, so queues/callbacks/user_permissions never got created and
#      the headline queue-routing demo 500'd on the first call.
#   3. Seed the first admin from ADMIN_EMAIL/ADMIN_PASSWORD if provided
#      (DEPLOY-C3 — no committed default password).
#   4. exec gunicorn (replaces PID 1 so signals/return codes propagate).
#
# The gunicorn invocation is passed as arguments to this script from compose,
# so the dev and prod overlays can each specify their own worker config.
set -e

echo "[entrypoint] waiting for redis..."
python wait-for-redis.py

# SKIP_BOOT_TASKS=1 keeps these short-lived app loads from starting the
# long-lived background tasks (queue monitor / fabric sync / watchdog / demo
# seed) — otherwise the migrate process would grab their Redis singleton locks
# and then exit, making the real gunicorn workers skip them. The var is scoped
# to each command so the gunicorn exec below runs WITHOUT it.
echo "[entrypoint] applying database migrations (flask db upgrade)..."
SKIP_BOOT_TASKS=1 flask db upgrade

echo "[entrypoint] seeding first admin (if configured)..."
SKIP_BOOT_TASKS=1 python seed_first_admin.py || echo "[entrypoint] admin seed step reported a non-fatal issue; continuing"

echo "[entrypoint] starting app: $*"
exec "$@"
