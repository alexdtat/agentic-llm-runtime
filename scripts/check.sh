#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME="agentic-check-$(date +%s)-$$"
export PROBE_TOKEN="cpu-test-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
export RUNTIME_PORT=0
export COMPOSE_BAKE=false
compose() { docker compose -f track_a/compose.yaml -f track_a/compose.test.yaml "$@"; }
out="results/$COMPOSE_PROJECT_NAME"
mkdir -p "$out"
cleanup() {
  compose logs --no-color > "$out/services.log" 2>&1 || true
  docker cp "$COMPOSE_PROJECT_NAME-checks:/results/." "$out/" 2>/dev/null || true
  docker rm -f "$COMPOSE_PROJECT_NAME-checks" >/dev/null 2>&1 || true
  compose --profile test down -v --remove-orphans --timeout 25
}
trap cleanup EXIT INT TERM
compose --profile test build
compose up -d --wait gateway
compose run --name "$COMPOSE_PROJECT_NAME-checks" --no-deps checks
compose run --rm --no-deps checks python3 -m track_a.restart_check prepare
compose kill -s SIGKILL gateway executor
compose up -d --wait gateway
compose run --rm --no-deps checks python3 -m track_a.restart_check verify
# Read actual daemon state; neither API status nor docker inspect failure proves cleanup.
leftovers=$(docker ps -aq --filter "label=agentic.scope=$COMPOSE_PROJECT_NAME")
[ -z "$leftovers" ] || { echo "Leaked sandbox containers: $leftovers" >&2; exit 1; }
printf '%s\n' '{"passed":true,"owned_sandbox_containers":0}' > "$out/cleanup.json"
