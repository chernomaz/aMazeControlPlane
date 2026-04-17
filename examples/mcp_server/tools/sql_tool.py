import os
import json
import logging
import mysql.connector
from mysql.connector import Error
from langchain_core.tools import tool
from langsmith import traceable

logger = logging.getLogger(__name__)

_DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "database": os.getenv("MYSQL_DATABASE", "mcp_demo"),
    "user": os.getenv("MYSQL_USER", "mcp_user"),
    "password": os.getenv("MYSQL_PASSWORD", "mcp_password"),
}


def _connect():
    return mysql.connector.connect(**_DB_CONFIG)


@tool
@traceable(name="sql_query")
def sql_query(query: str) -> str:
    """Execute a SQL query against the MySQL database and return the results.

    The database contains three tables:
    - users(id, name, email, city, age, is_active, created_at)
    - products(id, name, description, price, category, stock, created_at)
    - orders(id, user_id, product_id, quantity, total_price, status, created_at)

    Use SELECT queries to explore data. INSERT/UPDATE/DELETE are also supported.
    """
    logger.info("sql_query invoke query=%s", query[:200])
    try:
        conn = _connect()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query)

        if cursor.description:
            rows = cursor.fetchall()
            if not rows:
                return "Query returned 0 rows."
            # Serialize datetime objects to strings for JSON
            serializable = []
            for row in rows:
                serializable.append({k: str(v) if not isinstance(v, (int, float, bool, type(None), str)) else v for k, v in row.items()})
            result = json.dumps(serializable, indent=2, default=str)
            logger.info("sql_query returned %d rows", len(rows))
            return f"{len(rows)} row(s):\n{result}"
        else:
            conn.commit()
            affected = cursor.rowcount
            logger.info("sql_query affected %d rows", affected)
            return f"Query OK, {affected} row(s) affected."
    except Error as e:
        logger.error("sql_query error: %s", e)
        return f"SQL Error: {e}"
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass
