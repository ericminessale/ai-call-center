# DemoShop — bundled MCP gateway demo

This directory ships an MCP Gateway with a small e-commerce backend
(SQLite, seeded data) so the call center's **External Tools** feature
demos out of the box without any external setup.

After `docker-compose up`, the AI specialists already have these tools
available mid-call:

| Tool | What it does |
|---|---|
| `mcp_demoshop_find_customer_by_phone` | Customer lookup by caller-ID. |
| `mcp_demoshop_get_order` | Full order detail by order ID. |
| `mcp_demoshop_list_recent_orders` | A customer's recent order history. |
| `mcp_demoshop_track_shipment` | Synthetic carrier tracking timeline. |
| `mcp_demoshop_start_return` | Open an RMA against an order (writes to DB). |
| `mcp_demoshop_check_inventory` | Stock level for a SKU (sales calls). |

## Demo customers (preloaded)

The seed script writes these on first boot:

```
+15551234567  Eric Sample     (platinum) — 3 orders incl. one in transit
+15555678901  Alice Demo      (gold)     — 3 orders incl. one returned
+15558675309  Jenny Tutone    (standard) — 1 delivered order
+15550100100  Test Caller     (standard) — 1 in-transit order
+15552223344  Rita Returner   (gold)     — 1 pending order, 1 prior return
```

If your inbound test call doesn't come from one of these numbers, the AI
just won't find a match — it'll fall back to asking for an order number,
which it can look up directly with `get_order`.

## Configuration

Defaults in `docker-compose.yml`. Override via your `.env`:

```env
DEMO_MCP_USER=demo
DEMO_MCP_PASSWORD=demo
```

The gateway runs on port 8100 inside the docker network at hostname
`demo-mcp-gateway`. Other services reach it via
`http://demo-mcp-gateway:8100`. **Not exposed to the host** — the
gateway has no `ports:` mapping by design.

The first migration after install seeds an `McpGatewayConfig` row in
the backend pointing at this URL with the env credentials, bound to
the sales-ai and support-ai agents. You can edit / delete / rebind /
disable it from the **Settings → External Tools** admin tab; the
migration only seeds when no other gateways exist, so your changes
won't be undone on the next migration run.

## Replacing it for production

The whole point of MCP Gateway is that you bring your own. Two paths:

1. **Add another gateway** in the External Tools admin tab pointing at
   your real MCP gateway (Salesforce, Zendesk, internal services,
   whatever). Bind it to the agents that should see those tools.
   DemoShop can stay on for the dev/test environment, or you can
   disable / delete it.

2. **Replace DemoShop in place**: edit `demo-mcp/shop_mcp_server.py`
   to expose your own functions as MCP tools, edit
   `gateway-config.json` if you want to mount additional MCP servers,
   rebuild (`docker-compose up -d --build demo-mcp-gateway`). The
   admin row keeps pointing at the same URL.

For Salesforce specifically: wrap your existing
`simple_salesforce`-based client in `@mcp.tool()` decorators using the
same pattern as `shop_mcp_server.py`. See the writeup in
`HANDOFF_MCP_GATEWAY.md` (gitignored, project-internal) for a worked
example.

## Files

```
demo-mcp/
├── Dockerfile             # python:3.11-slim + mcp + signalwire[mcp-gateway]
├── requirements.txt
├── shop_seed.py           # creates + populates shop.db (idempotent)
├── shop_mcp_server.py     # the MCP server (six @mcp.tool() functions)
├── gateway-config.json    # config for the SDK's mcp-gateway CLI
├── entrypoint.sh          # seed-then-launch
└── README.md
```

DB lives at `/data/shop.db` inside the container, backed by the
`demo_mcp_data` named volume so seeded edits survive container
restarts. To reset, `docker-compose down -v` (drops all volumes) or
`docker volume rm signalwire-call-center_demo_mcp_data`.
