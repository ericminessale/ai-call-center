"""
shop_mcp_server.py — DemoShop MCP server.

Exposes a small e-commerce backend as MCP tools the AI can invoke
mid-call. This is the bundled demo for the call center's External
Tools feature: cloners replace this with their own MCP server when
they're ready (Salesforce, Zendesk, an internal pricing service,
whatever).

Speaks the MCP protocol over stdio. The SignalWire SDK's mcp-gateway
spawns this as a subprocess and bridges the tools to the AI agents.

Tools exposed:
  - find_customer_by_phone    look up a customer by their inbound caller-ID
  - get_order                 details + status of a specific order
  - list_recent_orders        a customer's recent orders (most recent first)
  - track_shipment            carrier + tracking + a synthetic in-transit timeline
  - start_return              kick off an RMA against an order
  - list_products             the catalog with prices, best sellers first (sales calls)
  - check_inventory           stock level for a SKU (sales calls)
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

DB_PATH = os.environ.get("SHOP_DB_PATH", "/app/shop.db")

mcp = FastMCP("demoshop")


# ---------------------------------------------------------------------------
# DB helpers — re-open per call to keep things simple. Tools are short-lived.
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _format_money(cents: int | None) -> str:
    if cents is None:
        return "$0.00"
    return f"${cents / 100:.2f}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def find_customer_by_phone(phone: str) -> dict:
    """Look up a customer by their phone number (E.164 like '+15551234567').

    Returns the customer's record (id, name, email, tier) when found, or
    ``{"found": false}`` when no match exists. Use this at the start of a
    call to personalize the conversation if the caller is in our system.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, phone, name, email, tier, created_at FROM customers WHERE phone = ?",
            (phone,),
        ).fetchone()
    if row is None:
        return {"found": False, "phone": phone}
    return {"found": True, **_row_to_dict(row)}


@mcp.tool()
def list_recent_orders(customer_id: int, limit: int = 5) -> list[dict]:
    """Return the customer's most recent orders, newest first.

    Each entry has order id, status, placed_at, total (formatted), and a
    short summary of the items. Cap ``limit`` to a reasonable number for
    voice — the AI typically wants the 3-5 most recent.
    """
    limit = max(1, min(limit, 25))  # clamp
    with _conn() as conn:
        orders = conn.execute(
            """SELECT id, status, total_cents, placed_at, tracking_number, carrier
                 FROM orders
                WHERE customer_id = ?
             ORDER BY placed_at DESC
                LIMIT ?""",
            (customer_id, limit),
        ).fetchall()
        out: list[dict] = []
        for o in orders:
            items = conn.execute(
                """SELECT p.name, oi.quantity
                     FROM order_items oi
                     JOIN products p ON p.sku = oi.sku
                    WHERE oi.order_id = ?""",
                (o["id"],),
            ).fetchall()
            out.append({
                "order_id": o["id"],
                "status": o["status"],
                "placed_at": o["placed_at"],
                "total": _format_money(o["total_cents"]),
                "carrier": o["carrier"],
                "tracking_number": o["tracking_number"],
                "items_summary": ", ".join(f"{i['quantity']}× {i['name']}" for i in items),
            })
        return out


@mcp.tool()
def get_order(order_id: int) -> dict:
    """Fetch full details on a specific order — status, items, shipping, total.

    Returns ``{"found": false}`` if the order doesn't exist.
    """
    with _conn() as conn:
        order = conn.execute(
            """SELECT o.*, c.name AS customer_name, c.phone AS customer_phone, c.tier AS customer_tier
                 FROM orders o
                 JOIN customers c ON c.id = o.customer_id
                WHERE o.id = ?""",
            (order_id,),
        ).fetchone()
        if order is None:
            return {"found": False, "order_id": order_id}

        items = conn.execute(
            """SELECT p.sku, p.name, oi.quantity, p.price_cents
                 FROM order_items oi
                 JOIN products p ON p.sku = oi.sku
                WHERE oi.order_id = ?""",
            (order_id,),
        ).fetchall()

        return {
            "found": True,
            "order_id": order["id"],
            "status": order["status"],
            "customer": {
                "id": order["customer_id"],
                "name": order["customer_name"],
                "phone": order["customer_phone"],
                "tier": order["customer_tier"],
            },
            "placed_at": order["placed_at"],
            "shipped_at": order["shipped_at"],
            "delivered_at": order["delivered_at"],
            "total": _format_money(order["total_cents"]),
            "carrier": order["carrier"],
            "tracking_number": order["tracking_number"],
            "items": [
                {
                    "sku": i["sku"],
                    "name": i["name"],
                    "quantity": i["quantity"],
                    "price": _format_money(i["price_cents"]),
                }
                for i in items
            ],
        }


@mcp.tool()
def track_shipment(order_id: int) -> dict:
    """Return shipping status + a synthetic carrier-style tracking timeline.

    For a delivered order: full timeline ending at delivery. For an
    in-transit order: progress to the most recent waypoint. For an
    un-shipped order: a polite "no tracking yet" response.
    """
    with _conn() as conn:
        order = conn.execute(
            """SELECT id, status, shipped_at, delivered_at, tracking_number, carrier
                 FROM orders WHERE id = ?""",
            (order_id,),
        ).fetchone()
    if order is None:
        return {"found": False, "order_id": order_id}

    if not order["shipped_at"]:
        return {
            "found": True,
            "order_id": order_id,
            "status": order["status"],
            "tracking_number": None,
            "carrier": None,
            "message": "Order has not shipped yet — no tracking available.",
            "events": [],
        }

    # Synthesize a believable timeline relative to shipped_at.
    shipped = datetime.fromisoformat(order["shipped_at"].rstrip("Z"))
    events = [
        {"at": shipped.isoformat() + "Z",                                "event": "Picked up by carrier"},
        {"at": (shipped + timedelta(hours=8)).isoformat() + "Z",         "event": "Departed origin facility"},
        {"at": (shipped + timedelta(hours=22)).isoformat() + "Z",        "event": "In transit"},
    ]
    if order["status"] == "delivered" and order["delivered_at"]:
        delivered = datetime.fromisoformat(order["delivered_at"].rstrip("Z"))
        events += [
            {"at": (delivered - timedelta(hours=4)).isoformat() + "Z",   "event": "Out for delivery"},
            {"at": delivered.isoformat() + "Z",                          "event": "Delivered"},
        ]
    elif order["status"] == "shipped":
        events.append(
            {"at": (shipped + timedelta(hours=36)).isoformat() + "Z",
             "event": "Arrived at destination facility — out for delivery soon"}
        )
    return {
        "found": True,
        "order_id": order_id,
        "status": order["status"],
        "tracking_number": order["tracking_number"],
        "carrier": order["carrier"],
        "events": events,
    }


@mcp.tool()
def start_return(order_id: int, reason: str) -> dict:
    """Open a return (RMA) against an order, with a short reason text.

    The RMA is written to the DB so a follow-up call (or the supervisor
    review log) can see it actually happened. Returns the new RMA
    number. Refuses to RMA an order that's already returned or
    cancelled, or one that hasn't shipped yet.
    """
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "error": "reason is required"}

    with _conn() as conn:
        order = conn.execute(
            "SELECT id, status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            return {"ok": False, "error": f"order {order_id} not found"}
        if order["status"] in {"returned", "cancelled"}:
            return {"ok": False, "error": f"order is already {order['status']}"}
        if order["status"] == "pending":
            return {"ok": False, "error": "order has not shipped — please cancel instead"}

        # Synth RMA number — predictable enough that the AI can read it back.
        existing = conn.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
        rma_number = f"RMA-2026-{existing + 1:04d}"
        # Random jitter for demo realism (so successive calls don't always
        # produce neatly sequential numbers).
        rma_number = f"RMA-2026-{existing + 1 + random.randint(0, 3):04d}"

        conn.execute(
            "INSERT INTO returns (order_id, rma_number, reason, created_at) VALUES (?, ?, ?, ?)",
            (order_id, rma_number, reason, datetime.utcnow().isoformat(timespec='seconds') + 'Z'),
        )
        conn.execute(
            "UPDATE orders SET status = 'returned' WHERE id = ?", (order_id,)
        )
        conn.commit()

    return {
        "ok": True,
        "order_id": order_id,
        "rma_number": rma_number,
        "instructions": (
            f"Return label for {rma_number} will be emailed to the customer. "
            "Drop the package at any carrier location within 14 days."
        ),
    }


def _availability(in_stock: int) -> str:
    return (
        "in stock" if in_stock > 5
        else "low stock" if in_stock > 0
        else "out of stock"
    )


@mcp.tool()
def list_products() -> dict:
    """What we sell: the product catalog with prices, stock, and which product
    is our most popular (best seller). Use this whenever a caller asks what
    products we offer, which product is most popular or best-selling, what
    something costs, or about pricing in general — it returns live catalog
    data, so never guess at products or prices.

    Returns ``most_popular`` (the best seller, by lifetime units sold) plus
    ``products``, the full lineup ranked best sellers first.
    """
    with _conn() as conn:
        rows = conn.execute(
            """SELECT sku, name, price_cents, in_stock, units_sold
                 FROM products
             ORDER BY units_sold DESC, sku""",
        ).fetchall()
    products = [
        {
            "sku": r["sku"],
            "name": r["name"],
            "price": _format_money(r["price_cents"]),
            "availability": _availability(r["in_stock"]),
            "units_sold": r["units_sold"],
        }
        for r in rows
    ]
    if not products:
        return {"found": False, "error": "catalog is empty"}
    top = products[0]
    return {
        "found": True,
        "most_popular": top,
        "products": products,
        # The AI reads this with the data. Callers interrupt long answers, so
        # the price must land in the first breath, not the third sentence.
        "answer_guidance": (
            "If the caller asked about the most popular product or its price, "
            f"your FIRST sentence must name product and price together — "
            f"'Our most popular product is the {top['name']} at {top['price']}.' "
            "Elaborate only after that sentence."
        ),
    }


@mcp.tool()
def check_inventory(sku: str) -> dict:
    """Stock level for a given SKU. Useful on sales calls.

    Returns ``{"found": false}`` when the SKU doesn't exist.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT sku, name, in_stock, price_cents FROM products WHERE sku = ?",
            (sku,),
        ).fetchone()
    if row is None:
        return {"found": False, "sku": sku}
    in_stock = row["in_stock"]
    return {
        "found": True,
        "sku": row["sku"],
        "name": row["name"],
        "price": _format_money(row["price_cents"]),
        "in_stock": in_stock,
        "availability": _availability(in_stock),
    }


if __name__ == "__main__":
    # FastMCP.run() defaults to stdio transport, which is what the
    # signalwire mcp-gateway expects when it spawns us as a subprocess.
    mcp.run()
