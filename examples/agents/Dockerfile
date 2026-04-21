# Demo-agent image. Built with repo root as context so we can reach both
# sdk/ and examples/agents/ in one COPY pass.
#
#   docker build -f examples/agents/Dockerfile \
#     --build-arg AGENT_MODULE=agent_sdk \
#     -t amaze/agent-sdk:dev .
#
# examples/compose.yml builds three containers off this file, one per
# agent module (agent_sdk, agent_sdk1, agent_sdk2).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SSL_CERT_FILE=/etc/amaze/ca/mitmproxy-ca-cert.pem \
    REQUESTS_CA_BUNDLE=/etc/amaze/ca/mitmproxy-ca-cert.pem

WORKDIR /agent

# --- aMaze SDK (editable so local iteration doesn't need a rebuild) ------
COPY sdk/ /sdk/
RUN pip install --no-cache-dir -e /sdk

# --- Agent deps (langchain, openai, etc.) --------------------------------
COPY examples/agents/requirements.txt /agent/requirements.txt
RUN pip install --no-cache-dir -r /agent/requirements.txt

# --- Agent source --------------------------------------------------------
COPY examples/agents/*.py /agent/

# Pick which agent module the container runs. Overriden per-service in
# examples/compose.yml.
ARG AGENT_MODULE=agent_sdk
ENV AGENT_MODULE=${AGENT_MODULE}

# --- User-facing chat + A2A listener ports -------------------------------
EXPOSE 8080 9002

CMD ["sh", "-c", "python /agent/${AGENT_MODULE}.py"]
