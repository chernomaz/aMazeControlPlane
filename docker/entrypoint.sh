#!/bin/sh
# Platform container entrypoint.
#
# Responsibilities:
#   1. Ensure expected directories exist (data dirs for redis + mitmproxy;
#      supervisord log dir + pid dir).
#   2. Hand off to supervisord.
#
# mitmproxy is launched with --set confdir=/opt/mitmproxy (see
# supervisord.conf), so its CA is written directly into the named volume
# at /opt/mitmproxy/mitmproxy-ca-cert.pem — no ~/.mitmproxy indirection.

set -eu

mkdir -p /data/redis /opt/mitmproxy /var/log/amaze /var/run

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
