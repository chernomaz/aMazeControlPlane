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
    - users(id, name, email, city, age, is_active, phone, credit_card, created_at)
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
                return json.dumps({"rows": [], "count": 0})
            # Serialize non-primitive types (datetime, Decimal) to strings for JSON.
            serializable = []
            for row in rows:
                serializable.append({
                    k: str(v) if not isinstance(v, (int, float, bool, type(None), str)) else v
                    for k, v in row.items()
                })
            # Return pure JSON. Row count is inline so the LLM can still see it
            # in the "count" field, and the proxy's PII redactor can parse the
            # whole response as JSON to redact leaf values individually.
            logger.info("sql_query returned %d rows", len(rows))
            return json.dumps({"count": len(rows), "rows": serializable}, indent=2, default=str)
        else:
            conn.commit()
            affected = cursor.rowcount
            logger.info("sql_query affected %d rows", affected)
            return json.dumps({"status": "ok", "affected": affected})
    except Error as e:
        logger.error("sql_query error: %s", e)
        return json.dumps({"error": str(e)})
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass
