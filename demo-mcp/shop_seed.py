"""
shop_seed.py — create + populate the DemoShop SQLite database.

Two modes:

  - default (``python3 shop_seed.py``) — idempotent first-boot seed:
    skips entirely if the DB file already exists. This is what the
    container entrypoint runs.
  - ``--force`` (``python3 shop_seed.py --force``) — restore an existing
    DB to seed state: drop, recreate and repopulate in ONE transaction,
    so a tool call landing mid-reset either waits for the lock or sees
    the finished result, never a half-empty shop. It also refreshes the
    relative order dates, which otherwise age forever after first boot.

The MCP server reads this DB read-mostly; only ``start_return`` writes
(it inserts a returns row and flips the order to 'returned'). So
without a periodic ``--force`` every caller's RMAs accumulate for the
life of the volume — on the hosted demo that's cross-visitor data
bleed, which is why ``shop_admin.py`` exposes the force path to the
nightly demo-reset cron.

Schema is deliberately minimal — enough to demo voice-driven order
lookups, status checks, and return creation, without simulating a full
e-commerce backend. Cloners replace this with their own MCP server when
they're ready.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any

DB_PATH = os.environ.get("SHOP_DB_PATH", "/app/shop.db")

# Seconds to wait out a concurrent tool call's lock before giving up. A
# reset is a handful of INSERTs, so anything near this is pathological.
SQLITE_TIMEOUT = 15.0


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

# Drop order for a re-seed: children first, so it stays correct if a
# future schema turns ``PRAGMA foreign_keys`` on. DROP TABLE takes the
# table's indexes with it, so they need no separate handling.
_TABLES_CHILD_FIRST = ("returns", "order_items", "orders", "products", "customers")


def _sql_statements(script: str) -> list[str]:
    """Split a DDL script into individual statements.

    ``Connection.executescript`` is unusable on the reset path: it issues
    an implicit COMMIT before running, which would break the
    single-transaction guarantee. SCHEMA has no semicolons inside string
    literals or comments, so a plain split is enough — keep it that way
    if you extend it.
    """
    return [stmt.strip() for stmt in script.split(';') if stmt.strip()]


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


def _populate(cur: sqlite3.Cursor) -> dict[str, int]:
    """Insert the seed rows into a freshly-created schema.

    The caller owns the transaction. Returns per-table row counts.
    """
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

    return {
        'customers': len(CUSTOMERS),
        'products': len(PRODUCTS),
        'orders': len(ORDERS),
        'returns': rma_serial - 1,
    }


def seed(force: bool = False) -> dict[str, Any]:
    """Create + populate the DB. Returns a summary dict.

    ``force=False`` (the first-boot path the entrypoint runs): no-op when
    the DB file already exists. ``force=True``: drop, recreate and
    repopulate in one transaction, restoring seed state over a live DB.
    """
    existed = os.path.exists(DB_PATH)
    if existed and not force:
        print(f"DemoShop DB already exists at {DB_PATH}; skipping seed.", flush=True)
        return {'seeded': False, 'reason': 'db_exists', 'db_path': DB_PATH}

    print(
        f"{'Re-seeding' if existed else 'Seeding'} DemoShop DB at {DB_PATH}…",
        flush=True,
    )

    # isolation_level=None turns off the driver's implicit transaction
    # handling so we can drive BEGIN/COMMIT ourselves, DDL included.
    # BEGIN IMMEDIATE takes the write lock up front instead of
    # discovering a conflict halfway through the rebuild.
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT, isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            for table in _TABLES_CHILD_FIRST:
                conn.execute(f'DROP TABLE IF EXISTS {table}')
            for stmt in _sql_statements(SCHEMA):
                conn.execute(stmt)
            counts = _populate(conn.cursor())
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()

    print(
        f"Seeded {counts['customers']} customers, {counts['products']} products, "
        f"{counts['orders']} orders.",
        flush=True,
    )
    print("Demo customers (callable phones for the AI to look up):", flush=True)
    for phone, name, _, tier in CUSTOMERS:
        print(f"  {phone}  {name}  ({tier})", flush=True)
    return {'seeded': True, 'reset': existed, 'db_path': DB_PATH, **counts}


if __name__ == "__main__":
    # --force / --reset restores an existing DB to seed state (see the
    # module docstring); no flag = the idempotent first-boot seed.
    seed(force=any(arg in ('--force', '--reset') for arg in sys.argv[1:]))
