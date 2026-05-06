#!/bin/sh
# Entrypoint for Chord DHT node.
# Reads Docker / docker-compose environment variables and translates them
# into the run_node.py CLI arguments that the argparse parser expects.

set -e

PORT="${CHORD_PORT:-5000}"
NODE_ID="${CHORD_ID:-}"
JOIN="${CHORD_JOIN:-}"
WORKERS="${CHORD_WORKERS:-4}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

# Build the argument list
ARGS="--host 0.0.0.0 --port ${PORT} --worker --workers ${WORKERS} --log ${LOG_LEVEL}"

# Optional: explicit node ID
if [ -n "${NODE_ID}" ]; then
  ARGS="${ARGS} --id ${NODE_ID}"
fi

# Optional: join an existing ring node
if [ -n "${JOIN}" ]; then
  ARGS="${ARGS} --join ${JOIN}"
fi

echo "[entrypoint] Starting Chord node: python run_node.py ${ARGS}"
exec python run_node.py ${ARGS}
