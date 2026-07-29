#!/bin/sh
# DemoShop MCP entrypoint:
#   1. ensure the SQLite DB exists + is seeded (idempotent)
#   2. start the shop-reset listener (hosted demo only — see below)
#   3. exec the SignalWire SDK's mcp-gateway pointed at our config
set -e

mkdir -p "$(dirname "$SHOP_DB_PATH")"

echo "[demo-mcp] seeding database (idempotent) at $SHOP_DB_PATH"
python3 /app/shop_seed.py

# The nightly demo-reset cron POSTs this to restore seed state, because
# the shop volume is mounted only in this container. shop_admin.py owns
# the hosted-demo gate (TENANCY_MODE/DEMO_MODE) so the flag is read in
# exactly one place per container: with the flag unset it logs a line and
# exits without binding, leaving 8100 as our only surface. Backgrounded
# rather than supervised — if it dies, the gateway keeps serving calls and
# the cron's next run reports the failure in the demo-reset logs.
python3 /app/shop_admin.py &

echo "[demo-mcp] launching mcp-gateway on port 8100"
exec mcp-gateway -c /app/gateway-config.json
