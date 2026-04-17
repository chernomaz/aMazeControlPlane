#!/usr/bin/env python3
"""Seed MySQL mcp_demo database with users, products, and orders tables (1000 rows each)."""

import random
import datetime
import mysql.connector
from mysql.connector import Error

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3306,
    "database": "mcp_demo",
    "user": "mcp_user",
    "password": "mcp_password",
}

FIRST_NAMES = ["Alice","Bob","Carol","David","Eve","Frank","Grace","Henry","Iris","Jack",
               "Karen","Leo","Mia","Nate","Olivia","Paul","Quinn","Rachel","Sam","Tina",
               "Uma","Victor","Wendy","Xander","Yara","Zane","Amy","Brian","Claire","Derek"]
LAST_NAMES  = ["Smith","Jones","Williams","Brown","Davis","Miller","Wilson","Moore","Taylor",
               "Anderson","Thomas","Jackson","White","Harris","Martin","Garcia","Martinez",
               "Robinson","Clark","Rodriguez","Lewis","Lee","Walker","Hall","Allen","Young"]
CITIES      = ["New York","Los Angeles","Chicago","Houston","Phoenix","Philadelphia","San Antonio",
               "San Diego","Dallas","San Jose","Austin","Jacksonville","Fort Worth","Columbus",
               "Charlotte","Indianapolis","San Francisco","Seattle","Denver","Nashville"]
CATEGORIES  = ["Electronics","Clothing","Books","Home & Garden","Sports","Toys","Food",
               "Beauty","Automotive","Office","Music","Pet Supplies","Health","Jewelry"]
ADJECTIVES  = ["Premium","Classic","Modern","Deluxe","Ultra","Pro","Standard","Basic",
                "Advanced","Smart","Eco","Compact","Portable","Wireless","Heavy-Duty"]
NOUNS       = ["Widget","Gadget","Gizmo","Device","Appliance","Tool","Instrument","Kit",
               "System","Module","Unit","Set","Pack","Bundle","Collection"]
ORDER_STATUSES = ["pending","processing","shipped","delivered","cancelled","refunded"]


def create_schema(cursor):
    cursor.execute("DROP TABLE IF EXISTS orders")
    cursor.execute("DROP TABLE IF EXISTS products")
    cursor.execute("DROP TABLE IF EXISTS users")

    cursor.execute("""
        CREATE TABLE users (
            id          INT PRIMARY KEY AUTO_INCREMENT,
            name        VARCHAR(100) NOT NULL,
            email       VARCHAR(150) NOT NULL UNIQUE,
            city        VARCHAR(100),
            age         TINYINT UNSIGNED,
            is_active   BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  DATETIME NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE products (
            id          INT PRIMARY KEY AUTO_INCREMENT,
            name        VARCHAR(200) NOT NULL,
            description TEXT,
            price       DECIMAL(10,2) NOT NULL,
            category    VARCHAR(100),
            stock       INT NOT NULL DEFAULT 0,
            created_at  DATETIME NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE orders (
            id          INT PRIMARY KEY AUTO_INCREMENT,
            user_id     INT NOT NULL,
            product_id  INT NOT NULL,
            quantity    SMALLINT UNSIGNED NOT NULL DEFAULT 1,
            total_price DECIMAL(10,2) NOT NULL,
            status      VARCHAR(30) NOT NULL DEFAULT 'pending',
            created_at  DATETIME NOT NULL,
            FOREIGN KEY (user_id)    REFERENCES users(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)


def seed_users(cursor, n=1000):
    base = datetime.datetime(2022, 1, 1)
    rows = []
    used_emails = set()
    i = 0
    while len(rows) < n:
        fn = random.choice(FIRST_NAMES)
        ln = random.choice(LAST_NAMES)
        name = f"{fn} {ln}"
        tag = random.randint(1, 9999)
        email = f"{fn.lower()}.{ln.lower()}{tag}@example.com"
        if email in used_emails:
            continue
        used_emails.add(email)
        city = random.choice(CITIES)
        age = random.randint(18, 75)
        is_active = random.random() > 0.1
        created_at = base + datetime.timedelta(days=random.randint(0, 1000), hours=random.randint(0, 23))
        rows.append((name, email, city, age, is_active, created_at))
    cursor.executemany(
        "INSERT INTO users (name, email, city, age, is_active, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        rows
    )
    print(f"  Inserted {n} users")


def seed_products(cursor, n=1000):
    base = datetime.datetime(2021, 1, 1)
    rows = []
    for i in range(n):
        adj = random.choice(ADJECTIVES)
        noun = random.choice(NOUNS)
        name = f"{adj} {noun} {i+1}"
        description = f"A {adj.lower()} {noun.lower()} for everyday use. SKU-{random.randint(10000,99999)}."
        price = round(random.uniform(1.99, 999.99), 2)
        category = random.choice(CATEGORIES)
        stock = random.randint(0, 500)
        created_at = base + datetime.timedelta(days=random.randint(0, 1500), hours=random.randint(0, 23))
        rows.append((name, description, price, category, stock, created_at))
    cursor.executemany(
        "INSERT INTO products (name, description, price, category, stock, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        rows
    )
    print(f"  Inserted {n} products")


def seed_orders(cursor, n=1000):
    base = datetime.datetime(2023, 1, 1)
    rows = []
    for _ in range(n):
        user_id = random.randint(1, 1000)
        product_id = random.randint(1, 1000)
        quantity = random.randint(1, 10)
        # fetch price inline via placeholder — we'll compute total from known price range
        price_per_unit = round(random.uniform(1.99, 999.99), 2)
        total_price = round(price_per_unit * quantity, 2)
        status = random.choice(ORDER_STATUSES)
        created_at = base + datetime.timedelta(days=random.randint(0, 365), hours=random.randint(0, 23))
        rows.append((user_id, product_id, quantity, total_price, status, created_at))
    cursor.executemany(
        "INSERT INTO orders (user_id, product_id, quantity, total_price, status, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        rows
    )
    print(f"  Inserted {n} orders")


def main():
    print("Connecting to MySQL...")
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    print("Creating schema...")
    create_schema(cursor)
    conn.commit()

    print("Seeding users...")
    seed_users(cursor)
    conn.commit()

    print("Seeding products...")
    seed_products(cursor)
    conn.commit()

    print("Seeding orders...")
    seed_orders(cursor)
    conn.commit()

    cursor.close()
    conn.close()
    print("Done! Database seeded successfully.")


if __name__ == "__main__":
    main()
