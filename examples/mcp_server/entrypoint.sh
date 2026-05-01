#!/bin/sh
# Register with aMaze orchestrator in the background, then start the MCP server.
# Registration retries until the orchestrator is up (AMAZE_REGISTER_RETRIES/DELAY).
# If AMAZE_ORCHESTRATOR_URL is unset, skip registration silently.
if [ -n "$AMAZE_ORCHESTRATOR_URL" ]; then
    python /usr/local/bin/amaze-mcp-register &
fi
exec python /mcp/server.py
