#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${HVAC_PYTHON:-python}"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
export API_URL="${API_URL:-http://127.0.0.1:8000}"
export CORS_ORIGINS="${CORS_ORIGINS:-http://127.0.0.1:5173,http://localhost:5173}"
# Fail before touching logs or starting a second worker for an existing API.
"$PYTHON_BIN" -c 'import socket,sys
for port in (int(sys.argv[1]),5222,9090):
    with socket.socket() as probe:
        probe.settimeout(.3)
        if probe.connect_ex(("127.0.0.1",port)) == 0:
            raise SystemExit(f"Port {port} is already in use. Stop the existing HVAC services before running dev.sh again.")' "${API_URL##*:}"
export PYTHONUNBUFFERED=1
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
mkdir -p results/logs
pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill -INT "$pid" 2>/dev/null || true; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
"$PYTHON_BIN" -m uvicorn hvac_mas.api.main:app --no-access-log --host 127.0.0.1 --port "${API_URL##*:}" > >(tee results/logs/backend.log) 2>&1 &
pids+=("$!")
wait_ready() {
  for ((attempt=0; attempt<100; attempt++)); do
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null || return 1; done
    if "$PYTHON_BIN" -c 'import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1] + "/health", timeout=1) as response:
    data = json.load(response)
    sys.exit(0 if data["status"] == "ok" and (sys.argv[2] == "api" or data["agent_ready"]) else 1)' "$API_URL" "$1" 2>/dev/null; then
      return 0
    fi
    for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null || return 1; done
    sleep .3
  done
  return 1
}
if ! wait_ready api; then
  message=$(sed -n 's/^hvac_mas\.storage\.MongoStartupError: //p' results/logs/backend.log)
  if [[ -n "$message" ]]; then
    printf '%s\n' "$message" >&2
  else
    tail -20 results/logs/backend.log >&2
  fi
  exit 1
fi
"$PYTHON_BIN" -m hvac_mas.runtime > >(tee results/logs/agents.log) 2>&1 &
pids+=("$!")
wait_ready agents || { tail -20 results/logs/agents.log; exit 1; }
printf 'Backend API %s\nFastAPI docs %s/docs\nBackend logs %s/results/logs\n' "$API_URL" "$API_URL" "$PWD"
while true; do
  for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null || { printf 'A service stopped. See results/logs.\n'; exit 1; }; done
  sleep 1
done
