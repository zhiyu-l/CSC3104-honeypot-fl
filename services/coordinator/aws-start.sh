#!/bin/sh
# Coordinator start-up on ECS Fargate (task definition command: /app/aws-start.sh).
#
# 1. Writes the TLS material from the task's Secrets Manager env vars to /tmp.
# 2. Starts the SuperLink (TLS, SuperNode authentication, state in memory).
# 3. In the background, re-registers the orgs' SuperNode public keys
#    (FL_SUPERNODE_PUBLIC_KEYS), because an in-memory SuperLink forgets them on
#    every restart; the clients then reconnect without anyone stepping in.
#    See src/fl_coordinator/register_nodes.py.
# 4. Forwards SIGTERM/SIGINT so ECS can still stop the SuperLink cleanly.
set -eu
umask 077
: "${FL_TLS_CA_CERT:?Missing CA certificate}"
: "${FL_TLS_SERVER_CERT:?Missing server certificate}"
: "${FL_TLS_SERVER_KEY:?Missing server private key}"
printf '%s\n' "$FL_TLS_CA_CERT" > /tmp/fl-ca.crt
printf '%s\n' "$FL_TLS_SERVER_CERT" > /tmp/fl-server.crt
printf '%s\n' "$FL_TLS_SERVER_KEY" > /tmp/fl-server.key

flower-superlink \
  --host 0.0.0.0 \
  --port 8000 \
  --ssl-ca-certfile /tmp/fl-ca.crt \
  --ssl-certfile /tmp/fl-server.crt \
  --ssl-keyfile /tmp/fl-server.key \
  --enable-supernode-auth --disable-runtime-dependency-installation &
superlink=$!
trap 'kill -TERM "$superlink" 2>/dev/null || true' TERM INT

# The Control API is on the same port; reach it locally under the certificate's name.
FL_ROOT_CERTIFICATES=/tmp/fl-ca.crt \
FL_HOST_OVERRIDE="${FL_TLS_NAME:-coordinator.fl.internal}=127.0.0.1" \
SUPERLINK_ADDRESS="${FL_TLS_NAME:-coordinator.fl.internal}:8000" \
  python -m fl_coordinator.register_nodes &

status=0
wait "$superlink" || status=$?
if kill -0 "$superlink" 2>/dev/null; then   # woken by the trap: wait for the real exit
  wait "$superlink" || status=$?
fi
exit "$status"
