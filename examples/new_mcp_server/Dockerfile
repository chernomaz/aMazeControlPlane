FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /mcp

COPY examples/new_mcp_server/requirements.txt /mcp/requirements.txt
RUN pip install --no-cache-dir -r /mcp/requirements.txt

COPY examples/new_mcp_server/ /mcp/
COPY scripts/amaze-mcp-register /usr/local/bin/amaze-mcp-register
RUN chmod +x /mcp/entrypoint.sh

ENV HOST=0.0.0.0 \
    PORT=8002

EXPOSE 8002

ENTRYPOINT ["/mcp/entrypoint.sh"]