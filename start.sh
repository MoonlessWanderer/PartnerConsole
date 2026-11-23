#!/usr/bin/env bash
set -euo pipefail
PP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$PP_ROOT/scripts/common.sh"
pp_prepare_runtime

lock="$PP_ROOT/runtime/pids/lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo "start or stop is already running." >&2
  exit 1
fi
trap 'rmdir "$lock" 2>/dev/null || true' EXIT

cd "$PP_ROOT"
pp_load_env_file "$PP_ROOT/config/api.env"
pp_reject_placeholders
python="$(pp_venv_python)"
if [[ ! -x "$PP_ROOT/node_modules/.bin/next" || ! -f "$PP_ROOT/.next/BUILD_ID" ]]; then
  echo "The web build is missing. Set config/web.env and run bash install.sh again." >&2
  exit 1
fi

if ! command -v ss >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  echo "ss and curl are required before starting." >&2
  exit 1
fi

if ! pp_claim_service api 8741 "$python" -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8741 --workers 2 --lifespan on \
  --proxy-headers --forwarded-allow-ips 127.0.0.1
then
  pp_abort_started_services "The API was not started."
  exit 1
fi
if ! pp_claim_service web 3741 "$PP_ROOT/node_modules/.bin/next" start -H 127.0.0.1 -p 3741; then
  pp_abort_started_services "The web app was not started."
  exit 1
fi
if ! pp_wait_for_ready api 8741 "http://127.0.0.1:8741/api/health" api; then
  pp_abort_started_services "API health did not succeed for this install."
  exit 1
fi
if ! pp_wait_for_ready web 3741 "http://127.0.0.1:3741/" web; then
  pp_abort_started_services "The web page did not become ready."
  exit 1
fi
echo "API health ok."
echo "Web health ok."
