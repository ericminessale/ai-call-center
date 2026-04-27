#!/bin/sh
# DemoShop MCP entrypoint:
#   1. ensure the SQLite DB exists + is seeded (idempotent)
#   2. exec the SignalWire SDK's mcp-gateway pointed at our config
set -e

mkdir -p "$(dirname "$SHOP_DB_PATH")"

echo "[demo-mcp] seeding database (idempotent) at $SHOP_DB_PATH"
python3 /app/shop_seed.py

echo "[demo-mcp] launching mcp-gateway on port 8100"
exec mcp-gateway -c /app/gateway-config.json
