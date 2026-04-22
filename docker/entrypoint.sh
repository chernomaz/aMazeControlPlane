#!/bin/sh
# Platform container entrypoint.
#
# Runs as root (required — supervisord needs it to drop privileges per
# program). The steps here are one-time setup that supervisord itself
# can't do:
#
#   1. Ensure data + log + runtime directories exist.
#   2. chown the volume mount points to the `amaze` user. Named Docker
#      volumes are created with root ownership on first mount; without a
#      chown here, the amaze user can't write to /data/redis or
#      /opt/mitmproxy after supervisord drops privileges.
#   3. Hand off to supervisord.
#
# mitmproxy is launched with --set confdir=/opt/mitmproxy, so its CA is
# written directly into the named volume at
# /opt/mitmproxy/mitmproxy-ca-cert.pem.

set -eu

mkdir -p /data/redis /opt/mitmproxy /var/log/amaze /var/run
chown -R amaze:amaze /data /opt/mitmproxy /var/log/amaze

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
