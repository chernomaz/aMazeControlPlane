# Demo MCP-server image.
#
#   docker build -f examples/mcp_server/Dockerfile -t amaze/demo-mcp:dev .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /mcp

COPY examples/mcp_server/requirements.txt /mcp/requirements.txt
RUN pip install --no-cache-dir -r /mcp/requirements.txt

COPY examples/mcp_server/ /mcp/
COPY scripts/amaze-mcp-register /usr/local/bin/amaze-mcp-register
RUN chmod +x /mcp/entrypoint.sh

# No SSL_CERT_FILE override — MCP tools reach the public internet
# directly (e.g. api.tavily.com) and must trust real public CAs. The
# mitmproxy CA is NOT needed here: incoming agent→MCP traffic is
# server-accepted TCP (no client-cert verification on our end); MCP
# outbound bypasses our proxy in step 1.
ENV HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

ENTRYPOINT ["/mcp/entrypoint.sh"]
