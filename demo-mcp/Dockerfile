# DemoShop MCP — bundled demo for the External Tools feature.
#
# Runs the SignalWire SDK's mcp-gateway service with a single MCP server
# attached (shop_mcp_server.py). Pre-seeds an SQLite DB with example
# customers, products, orders, and returns on first boot so the feature
# is demoable end-to-end with zero external setup.
#
# All deps come from public PyPI (signalwire-sdk, mcp) — a fresh clone
# of the repo builds this image without any local-only files.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shop_seed.py shop_mcp_server.py gateway-config.json entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# Persistent SQLite DB lives here. Mount a named volume in compose to
# survive restarts; otherwise the seed reruns on every fresh container.
ENV SHOP_DB_PATH=/data/shop.db

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f -u "${DEMO_MCP_USER:-demo}:${DEMO_MCP_PASSWORD:-demo}" \
        http://localhost:8100/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
