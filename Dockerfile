# Single-container aMaze platform image: jaeger + orchestrator + proxy under
# supervisord. Built from the repo root; see docker/docker-compose.yml for
# the runtime wiring (networks, CA volume). Redis runs in its own container.
#
#   docker build -f Dockerfile -t amaze/platform:dev .

FROM python:3.12-slim

# --- System deps: supervisord, tools for diagnostics ----------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      supervisor \
      curl \
      wget \
 && rm -rf /var/lib/apt/lists/*

RUN wget -qO /tmp/jaeger.tar.gz \
      https://github.com/jaegertracing/jaeger/releases/download/v1.57.0/jaeger-1.57.0-linux-amd64.tar.gz \
 && tar -xzf /tmp/jaeger.tar.gz -C /tmp/ \
 && mv /tmp/jaeger-1.57.0-linux-amd64/jaeger-all-in-one /usr/local/bin/jaeger-all-in-one \
 && chmod +x /usr/local/bin/jaeger-all-in-one \
 && rm -rf /tmp/jaeger.tar.gz /tmp/jaeger-1.57.0-linux-amd64

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- Python deps ----------------------------------------------------------
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- Platform code + config ----------------------------------------------
# Services and config are copied whole — orchestrator and proxy share the
# same venv under /usr/local/lib/python3.12/site-packages.
COPY services/ /app/services/
COPY config/   /app/config/

# --- supervisord + entrypoint --------------------------------------------
COPY docker/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/entrypoint.sh    /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# --- Non-root user --------------------------------------------------------
# Orchestrator, proxy and redis all run as `amaze` (uid 1000) — supervisord
# stays root so it can drop privileges per program and write to the shared
# log dir. Dropping root is defense-in-depth: a compromised addon/process
# can't escape to the container filesystem or tamper with other programs'
# state under /data.
RUN groupadd --system amaze \
 && useradd --system --gid amaze --uid 1000 --home /app --shell /bin/sh amaze

# --- Runtime layout -------------------------------------------------------
# Data dirs for redis AOF + mitmproxy CA. These are declared as VOLUMEs so
# that named volumes (declared in docker/docker-compose.yml) can persist
# state across container restarts. Ownership handed to the amaze user.
RUN mkdir -p /opt/mitmproxy /var/log/amaze /data/jaeger/data /data/jaeger/keys \
 && chown -R amaze:amaze /opt/mitmproxy /var/log/amaze /app /data
VOLUME ["/opt/mitmproxy"]

ENV HOME=/opt/mitmproxy

EXPOSE 8001 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
