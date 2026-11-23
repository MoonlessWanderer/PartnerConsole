#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
pp_prepare_runtime
cd "$PP_ROOT"
pp_load_env_file "$PP_ROOT/config/api.env"
pp_reject_placeholders
pp_confirm_database "${1:-}"
if [[ -z "${2:-}" ]]; then
  echo "Usage: bash scripts/reset-admin-password.sh --confirm-database USERNAME" >&2
  exit 1
fi
python="$(pp_venv_python)"
(cd "$PP_ROOT" && "$python" -m app.reset_admin_password "$2")
