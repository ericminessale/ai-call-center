"""
shop_seed.py — create + populate the DemoShop SQLite database.

Idempotent: skips if the DB file already exists. To regenerate from
scratch, delete shop.db and rerun. The MCP server reads from this DB
read-mostly; only ``start_return`` writes (it inserts a returns row).

Schema is deliberately minimal — enough to demo voice-driven order
lookups, status checks, and return creation, without simulating a full
e-commerce backend. Cloners replace this with their own MCP server when
they're ready.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.environ.get("SHOP_DB_PATH", "/app/shop.db")


SCHEMA = """
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT UNIQUE NOT NULL,        -- E.164: '+15551234567'
    name        TEXT NOT NULL,
    email       TEXT,
    tier        TEXT NOT NULL DEFAULT 'standard',  -- standard | gold | platinum
    created_at  TEXT NOT NULL
);

CREATE TABLE products (
    sku         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    in_stock    INTEGER NOT NULL DEFAULT 0   -- units on hand
);

CREATE TABLE orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    status          TEXT NOT NULL,           -- pending | shipped | delivered | returned | cancelled
    total_cents     INTEGER NOT NULL,
    placed_at       TEXT NOT NULL,
    shipped_at      TEXT,
    delivered_at    TEXT,
    tracking_number TEXT,
    carrier         TEXT
);

CREATE TABLE order_items (
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    sku         TEXT NOT NULL REFERENCES products(sku),
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (order_id, sku)
);

CREATE TABLE returns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders(id),
    rma_number   TEXT UNIQUE NOT NULL,
    reason       TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX idx_customers_phone ON customers(phone);
CREATE INDEX idx_orders_customer ON orders(customer_id);
"""


# Ten days ago, in ISO format — used for relative dates in seed data.
def _days_ago(n: int) -> str:
    return (datetime.utcnow() - timedelta(days=n)).isoformat(timespec='seconds') + 'Z'


CUSTOMERS = [
    # phone, name, email, tier
    ("+15551234567", "Eric Sample",   "eric@example.com",   "platinum"),
    ("+15555678901", "Alice Demo",    "alice@example.com",  "gold"),
    ("+15558675309", "Jenny Tutone",  "jenny@example.com",  "standard"),
    ("+15550100100", "Test Caller",   "test@example.com",   "standard"),
    ("+15552223344", "Rita Returner", "rita@example.com",   "gold"),
]

PRODUCTS = [
    # sku, name, price_cents, in_stock
    ("HDPH-001",  "Wireless Over-Ear Headphones",        14999, 42),
    ("HDPH-002",  "True Wireless Earbuds",                7999, 0),   # out of stock
    ("CABL-USBC", "USB-C Charging Cable, 2m",             1299, 318),
    ("STND-LAP",  "Adjustable Laptop Stand",              4999, 17),
    ("KEYB-MECH", "Mechanical Keyboard, Tactile",        12900, 8),
    ("MOUS-ERG",  "Ergonomic Vertical Mouse",             6499, 0),   # out of stock
    ("WBCM-4K",   "4K Webcam, Auto-focus",                9900, 23),
    ("HUB-USB",   "7-Port USB Hub w/ Power",              3499, 60),
]

# Orders are written in chronological seed groups so we can reference
# the right customer_id by ordinal (1-indexed). (customer_idx_1based,
# status, days_ago, items=[(sku, qty)], carrier, tracking_number)
ORDERS = [
    # Eric — platinum, frequent buyer
    (1, "delivered", 28, [("KEYB-MECH", 1), ("CABL-USBC", 2)], "UPS",   "1Z999AA10123456784"),
    (1, "delivered", 14, [("HDPH-001", 1)],                   "FedEx", "775598284156"),
    (1, "shipped",    3, [("STND-LAP", 1), ("HUB-USB", 1)],   "UPS",   "1Z999AA10123456900"),
    # Alice — gold, returning customer (literally — has a return)
    (2, "delivered", 35, [("WBCM-4K", 1)],                    "FedEx", "775598284200"),
    (2, "returned",  21, [("HDPH-002", 1)],                   "UPS",   "1Z999AA10123456850"),
    (2, "shipped",    2, [("MOUS-ERG", 1), ("CABL-USBC", 1)], "UPS",   "1Z999AA10123456901"),
    # Jenny — standard, one recent order
    (3, "delivered", 10, [("HDPH-001", 1), ("CABL-USBC", 3)], "USPS",  "9400111202555842761234"),
    # Test Caller — for cloners' synthetic test calls
    (4, "shipped",    1, [("HUB-USB", 2)],                    "UPS",   "1Z999AA10123457001"),
    # Rita — has a pending order + previous return
    (5, "pending",    0, [("KEYB-MECH", 1), ("WBCM-4K", 1)],  None,    None),
    (5, "returned",  45, [("HDPH-001", 1)],                   "FedEx", "775598284001"),
]


def seed() -> None:
    if os.path.exists(DB_PATH):
        print(f"DemoShop DB already exists at {DB_PATH}; skipping seed.", flush=True)
        return

    print(f"Seeding DemoShop DB at {DB_PATH}…", flush=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    # Customers
    now = _days_ago(0)
    for phone, name, email, tier in CUSTOMERS:
        cur.execute(
            "INSERT INTO customers (phone, name, email, tier, created_at) VALUES (?, ?, ?, ?, ?)",
            (phone, name, email, tier, now),
        )

    # Products
    for sku, name, price_cents, in_stock in PRODUCTS:
        cur.execute(
            "INSERT INTO products (sku, name, price_cents, in_stock) VALUES (?, ?, ?, ?)",
            (sku, name, price_cents, in_stock),
        )

    # Orders + items + returns (where applicable)
    rma_serial = 1
    for cust_idx, status, days_ago, items, carrier, tracking in ORDERS:
        placed = _days_ago(days_ago)
        # Derive shipped/delivered timestamps from the status.
        shipped_at = _days_ago(max(days_ago - 1, 0)) if status in {
            "shipped", "delivered", "returned"
        } else None
        delivered_at = _days_ago(max(days_ago - 3, 0)) if status in {
            "delivered", "returned"
        } else None

        # Compute total from product prices.
        prices = {sku: price for sku, _, price, _ in PRODUCTS}
        total = sum(prices[sku] * qty for sku, qty in items)

        cur.execute(
            """INSERT INTO orders
               (customer_id, status, total_cents, placed_at, shipped_at,
                delivered_at, tracking_number, carrier)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cust_idx, status, total, placed, shipped_at, delivered_at, tracking, carrier),
        )
        order_id = cur.lastrowid
        for sku, qty in items:
            cur.execute(
                "INSERT INTO order_items (order_id, sku, quantity) VALUES (?, ?, ?)",
                (order_id, sku, qty),
            )
        if status == "returned":
            cur.execute(
                "INSERT INTO returns (order_id, rma_number, reason, created_at) VALUES (?, ?, ?, ?)",
                (order_id, f"RMA-2026-{rma_serial:04d}", "demo seed: customer changed mind",
                 _days_ago(max(days_ago - 5, 0))),
            )
            rma_serial += 1

    conn.commit()
    conn.close()
    print(
        f"Seeded {len(CUSTOMERS)} customers, {len(PRODUCTS)} products, "
        f"{len(ORDERS)} orders.",
        flush=True,
    )
    print("Demo customers (callable phones for the AI to look up):", flush=True)
    for phone, name, _, tier in CUSTOMERS:
        print(f"  {phone}  {name}  ({tier})", flush=True)


if __name__ == "__main__":
    seed()
