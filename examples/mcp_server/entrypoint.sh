#!/bin/sh
set -e

# ── MySQL ──────────────────────────────────────────────────────────────────────
# Create the socket directory that MariaDB expects.
mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld

# Initialize the data directory on first boot (not baked into the image).
if [ ! -d /var/lib/mysql/mysql ]; then
    echo "[entrypoint] Initializing MySQL data directory..."
    mysql_install_db --user=mysql --datadir=/var/lib/mysql > /dev/null
fi

echo "[entrypoint] Starting MySQL..."
mysqld --user=mysql &

echo "[entrypoint] Waiting for MySQL to be ready..."
for i in $(seq 1 30); do
    if mysqladmin --user=root ping --silent 2>/dev/null; then
        echo "[entrypoint] MySQL ready."
        break
    fi
    sleep 1
done

# Create database and user (idempotent).
mysql --user=root <<'SQL'
CREATE DATABASE IF NOT EXISTS mcp_demo CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'mcp_user'@'%' IDENTIFIED BY 'mcp_password';
GRANT ALL PRIVILEGES ON mcp_demo.* TO 'mcp_user'@'%';
FLUSH PRIVILEGES;
SQL

# Seed only when the users table is absent or empty (skip on container restart).
if ! mysql --user=root mcp_demo -e "SELECT 1 FROM users LIMIT 1" 2>/dev/null; then
    echo "[entrypoint] Seeding database (3 × 1000 rows)..."
    python /mcp/seed_mysql.py
else
    echo "[entrypoint] Database already seeded — skipping."
fi

# ── aMaze registration ─────────────────────────────────────────────────────────
if [ -n "$AMAZE_ORCHESTRATOR_URL" ]; then
    python /usr/local/bin/amaze-mcp-register &
fi

# ── MCP server ─────────────────────────────────────────────────────────────────
exec python /mcp/server.py
