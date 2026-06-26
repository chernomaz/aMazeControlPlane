#!/bin/sh
set -e

# ── MySQL setup (runs in the background, in parallel with the MCP server) ──────
# The MCP server's DB tools connect LAZILY and fail gracefully (sql_tool.py
# only opens a connection when sql_query is actually invoked, and returns a
# "SQL Error" string on failure). Tool *discovery* (get_tools) needs no DB.
# So we start the MCP server immediately and bring MySQL up alongside it —
# this removes the boot race where agents call get_tools() during the ~30s
# MySQL init/seed and get nothing. With the persistent /var/lib/mysql volume,
# init + seed happen only on first boot; later boots skip straight through.
setup_mysql() {
    # Create the socket directory that MariaDB expects.
    mkdir -p /run/mysqld && chown mysql:mysql /run/mysqld

    # Initialize the data directory on first boot (persisted via volume).
    if [ ! -d /var/lib/mysql/mysql ]; then
        echo "[entrypoint] Initializing MySQL data directory..."
        mysql_install_db --user=mysql --datadir=/var/lib/mysql > /dev/null
    fi

    echo "[entrypoint] Starting MySQL..."
    mysqld --user=mysql &

    echo "[entrypoint] Waiting for MySQL to be ready..."
    for i in $(seq 1 60); do
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

    # Seed only when the users table is absent or empty (skip on later boots).
    if ! mysql --user=root mcp_demo -e "SELECT 1 FROM users LIMIT 1" 2>/dev/null; then
        echo "[entrypoint] Seeding database (3 × 1000 rows)..."
        python /mcp/seed_mysql.py
    else
        echo "[entrypoint] Database already seeded — skipping."
    fi

    echo "[entrypoint] MySQL setup complete."
}

# Run the whole MySQL bring-up in the background so it doesn't block the
# server, and so a seed failure can't take the container down (the server
# stays useful for non-DB tools).
setup_mysql &

# ── aMaze registration ─────────────────────────────────────────────────────────
# Independent of MySQL — registers the MCP server endpoint with the orchestrator.
if [ -n "$AMAZE_ORCHESTRATOR_URL" ]; then
    python /usr/local/bin/amaze-mcp-register &
fi

# ── MCP server ─────────────────────────────────────────────────────────────────
# Foreground (PID 1) — starts listening immediately so agents' get_tools()
# succeeds even while MySQL is still coming up in the background.
exec python /mcp/server.py
