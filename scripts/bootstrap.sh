#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
pp_prepare_runtime
cd "$PP_ROOT"
pp_load_env_file "$PP_ROOT/config/api.env"
pp_reject_placeholders
pp_confirm_database "${1:-}"
python="$(pp_venv_python)"
if [[ "${2:-}" == "--password-stdin" ]]; then
  if [[ -t 0 ]] || ! IFS= read -r ADMIN_PASSWORD || [[ -z "$ADMIN_PASSWORD" ]]; then
    echo "A nonempty initial admin password is required on non-terminal standard input." >&2
    exit 1
  fi
elif [[ -z "${ADMIN_PASSWORD:-}" ]]; then
  if [[ ! -t 0 ]]; then
    echo "Set nothing in the config file. Run this from a terminal so the admin password can be requested without saving it." >&2
    exit 1
  fi
  read -r -s -p "Initial admin password (not saved): " ADMIN_PASSWORD
  echo
  read -r -s -p "Repeat password: " repeated
  echo
  if [[ "$ADMIN_PASSWORD" != "$repeated" ]]; then
    echo "Passwords do not match." >&2
    exit 1
  fi
  unset repeated
fi
export ADMIN_PASSWORD
(cd "$PP_ROOT" && "$python" -m app.bootstrap_database)
status=$?
unset ADMIN_PASSWORD
exit "$status"
