#!/usr/bin/env bash
set -euo pipefail
PP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PP_ROOT/scripts/common.sh"

pp_status_one() {
  local name="$1"
  local pidfile="$PP_ROOT/runtime/pids/${name}.pid"
  if pp_leader_matches "$name"; then
    echo "$name: running (${PP_ID_PID})"
    return
  fi
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(tr -d '[:space:]' < "$pidfile")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      echo "$name: pid file is not a process of this install"
      return
    fi
  fi
  echo "$name: stopped"
}

echo "install: $PP_ROOT"
pp_status_one api
pp_status_one web
if ! command -v ss >/dev/null 2>&1; then
  echo "health: not checked; ss is unavailable"
elif pp_port_owned_by_service api 8741 && pp_http_matches "http://127.0.0.1:8741/api/health" api; then
  echo "health: ok"
elif pp_port_owned_by_service api 8741; then
  echo "health: not reachable"
elif pp_port_listening 8741; then
  echo "health: not checked; port 8741 belongs to another program"
else
  echo "health: not checked; this install is not listening"
fi
