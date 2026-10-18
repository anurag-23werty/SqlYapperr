"""Deterministic sample database.

Run with ``python -m app.database.seed [path]``. The same seed always produces
byte-for-byte identical data, so tests can assert on concrete values.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 20260101

SCHEMA = """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    email       TEXT    NOT NULL UNIQUE,
    city        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE TABLE products (
    product_id   INTEGER PRIMARY KEY,
    product_name TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    price        REAL    NOT NULL CHECK (price >= 0)
);
CREATE TABLE orders (
    order_id     INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date   TEXT    NOT NULL,
    total_amount REAL    NOT NULL CHECK (total_amount >= 0),
    status       TEXT    NOT NULL CHECK (status IN ('completed','shipped','cancelled','returned'))
);
CREATE TABLE order_items (
    order_id   INTEGER NOT NULL REFERENCES orders(order_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL    NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (order_id, product_id)
);
-- Sensitive table that exists but must never be readable through the agent.
CREATE TABLE admin_users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_items_product ON order_items(product_id);
"""

FIRST = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Isha", "Arjun", "Sara", "Vikram", "Anaya",
         "Liam", "Emma", "Noah", "Olivia", "Mateo", "Sofia", "Yuki", "Chen", "Amara", "Omar"]
LAST = ["Sharma", "Patel", "Iyer", "Khan", "Gupta", "Reddy", "Smith", "Garcia", "Kim", "Okafor"]
CITIES = ["Bengaluru", "Mumbai", "Delhi", "Pune", "Hyderabad", "Chennai", "London", "Berlin"]
CATALOG = [
    ("Laptop Pro 14", "Electronics", 1299.00), ("Wireless Mouse", "Electronics", 25.50),
    ("Mechanical Keyboard", "Electronics", 89.99), ("4K Monitor", "Electronics", 349.00),
    ("USB-C Hub", "Electronics", 39.90), ("Noise-Cancelling Headphones", "Electronics", 199.00),
    ("Office Chair", "Furniture", 249.00), ("Standing Desk", "Furniture", 499.00),
    ("Desk Lamp", "Furniture", 34.00), ("Bookshelf", "Furniture", 129.00),
    ("Coffee Beans 1kg", "Grocery", 18.75), ("Green Tea 100ct", "Grocery", 9.99),
    ("Protein Bars x12", "Grocery", 21.00), ("Running Shoes", "Apparel", 110.00),
    ("Rain Jacket", "Apparel", 85.00), ("Cotton T-Shirt", "Apparel", 15.00),
    ("Yoga Mat", "Sports", 29.00), ("Dumbbell Set", "Sports", 75.00),
    ("Water Bottle", "Sports", 12.50), ("Backpack 30L", "Travel", 64.00),
]
STATUSES = ["completed"] * 70 + ["shipped"] * 15 + ["cancelled"] * 7 + ["returned"] * 8


def build(conn: sqlite3.Connection, seed: int = SEED) -> None:
    rng = random.Random(seed)
    conn.executescript(SCHEMA)

    customers = []
    for cid in range(1, 61):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        created = date(2023, 1, 1) + timedelta(days=rng.randint(0, 700))
        customers.append((cid, name, f"customer{cid}@example.com", rng.choice(CITIES), created.isoformat()))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?)", customers)

    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?)",
        [(pid, n, c, p) for pid, (n, c, p) in enumerate(CATALOG, start=1)],
    )

    order_id = 0
    start = date(2024, 1, 1)
    for _ in range(600):
        order_id += 1
        cid = rng.randint(1, 60)
        odate = start + timedelta(days=rng.randint(0, 730))  # 2024-01-01 .. 2025-12-31
        status = rng.choice(STATUSES)
        items = rng.sample(range(1, len(CATALOG) + 1), k=rng.randint(1, 4))
        total = 0.0
        rows = []
        for pid in items:
            qty = rng.randint(1, 3)
            unit = CATALOG[pid - 1][2]
            total += qty * unit
            rows.append((order_id, pid, qty, unit))
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?)",
            (order_id, cid, odate.isoformat(), round(total, 2), status),
        )
        conn.executemany("INSERT INTO order_items VALUES (?,?,?,?)", rows)

    conn.executemany(
        "INSERT INTO admin_users VALUES (?,?,?)",
        [(1, "root", "pbkdf2_sha256$600000$FAKE$notarealhash"), (2, "ops", "pbkdf2_sha256$600000$FAKE$alsofake")],
    )
    conn.commit()


def seed_database(path: Path, *, overwrite: bool = False, seed: int = SEED) -> Path:
    path = Path(path)
    if path.exists():
        if not overwrite:
            return path
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        build(conn, seed)
    finally:
        conn.close()
    tmp.replace(path)  # atomic: readers never see a half-built database
    return path


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("data/sqlyapper.db")
    seed_database(target, overwrite=True)
    print(f"Seeded database at {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
