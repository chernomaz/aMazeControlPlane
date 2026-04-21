# Single-container aMaze platform image: redis + orchestrator + proxy under
# supervisord. Built from the repo root; see docker/docker-compose.yml for
# the runtime wiring (networks, CA volume, Redis AOF volume).
#
#   docker build -f Dockerfile -t amaze/platform:dev .

FROM python:3.12-slim

# --- System deps: redis, supervisord, tools for diagnostics ---------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      redis-server \
      supervisor \
      curl \
 && rm -rf /var/lib/apt/lists/*

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

# --- Runtime layout -------------------------------------------------------
# Data dirs for redis AOF + mitmproxy CA. These are declared as VOLUMEs so
# that named volumes (declared in docker/docker-compose.yml) can persist
# state across container restarts.
RUN mkdir -p /data/redis /opt/mitmproxy /var/log/amaze
VOLUME ["/data/redis", "/opt/mitmproxy"]

ENV HOME=/opt/mitmproxy

EXPOSE 8001 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
