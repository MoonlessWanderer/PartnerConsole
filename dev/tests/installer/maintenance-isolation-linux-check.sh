#!/usr/bin/env bash
set -euo pipefail
[[ "$EUID" == 0 ]]
source_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/scripts/common.sh"
work="$(mktemp -d /var/tmp/partnerconsole-maintenance-test.XXXXXXXX)"
trap 'rm -rf -- "$work"' EXIT
chmod 755 "$work"
install -m 644 "$source_file" "$work/common.sh"
PP_ROOT="$work/app"
export PP_ROOT
install -d -m 755 "$PP_ROOT"
install -d -m 700 -o nobody -g "$(id -gn nobody)" "$PP_ROOT/runtime"
install -d -m 700 -o nobody -g "$(id -gn nobody)" "$PP_ROOT/runtime/cache"

# Test the paths chosen by both execution identities and their effective permissions.
# shellcheck source=scripts/common.sh
source "$work/common.sh"
pp_prepare_runtime
maintenance="$PP_ROOT/.release/maintenance"
[[ "$TMPDIR" == "$maintenance/tmp" ]]
[[ "$PYTHONPYCACHEPREFIX" == "$maintenance/cache/pyc" ]]
[[ "$PIP_CACHE_DIR" == "$maintenance/cache/pip" ]]
[[ "$npm_config_cache" == "$maintenance/cache/npm" ]]
[[ "$(stat -c '%u:%a' "$maintenance")" == 0:700 ]]
[[ ! -e "$PP_ROOT/runtime/cache/pyc" ]]
! runuser -u nobody -- test -w "$maintenance"

install -d -m 700 -o nobody -g "$(id -gn nobody)" "$PP_ROOT/data" "$PP_ROOT/logs"
runuser -u nobody -- bash -c '
  set -euo pipefail
  source "$1"
  pp_prepare_runtime
  [[ "$TMPDIR" == "$PP_ROOT/runtime/tmp" ]]
  [[ "$PYTHONPYCACHEPREFIX" == "$PP_ROOT/runtime/cache/pyc" ]]
' bash "$work/common.sh"

install -d -m 750 "$PP_ROOT/.next"
install -d -m 700 -o nobody -g "$(id -gn nobody)" "$PP_ROOT/.next/cache"
touch "$PP_ROOT/.next/BUILD_ID"
pp_prepare_web_build
[[ ! -e "$PP_ROOT/.next/cache" && -f "$PP_ROOT/.next/BUILD_ID" ]]
echo 'Root maintenance is isolated from service temp, Python, npm, pip and generated Next.js caches.'
