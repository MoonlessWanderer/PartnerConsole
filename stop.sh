#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"

pp_stop_one() {
  pp_stop_service "$1"
}

pp_stop_main() {
  PP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  pp_prepare_runtime

  # The EXIT trap runs after this function returns, so the lock path cannot be local.
  PP_STOP_LOCK="$PP_ROOT/runtime/pids/lock"
  if ! mkdir "$PP_STOP_LOCK" 2>/dev/null; then
    echo "start or stop is already running." >&2
    exit 1
  fi
  trap 'rmdir "$PP_STOP_LOCK" 2>/dev/null || true' EXIT
  pp_stop_one web
  pp_stop_one api
}

if [[ "${PARTNERCONSOLE_LIBRARY:-}" != "1" ]]; then
  pp_stop_main
fi
