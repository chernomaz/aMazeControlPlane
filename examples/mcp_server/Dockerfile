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

# Runtime cert bundle — see note in agents/Dockerfile. Set after pip
# install so the build itself can verify TLS against pypi.
ENV SSL_CERT_FILE=/etc/amaze/ca/mitmproxy-ca-cert.pem \
    REQUESTS_CA_BUNDLE=/etc/amaze/ca/mitmproxy-ca-cert.pem \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

CMD ["python", "/mcp/server.py"]
