#!/usr/bin/env bash
# Runs only on a disposable Linux host with the fixture MySQL container.
set -euo pipefail
umask 077

source_tree="${1:?provide the source tree to install}"
mode="${2:?provide baseline or fixed}"
[[ $(uname -s) == Linux && "$EUID" == 0 ]] || { echo 'This test requires disposable Linux as root.' >&2; exit 1; }
[[ "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]] || { echo 'Set PARTNERCONSOLE_DISPOSABLE_HOST=1 on a disposable host only.' >&2; exit 1; }
[[ ! -e /usr/local/bin/partnerconsole && ! -L /usr/local/bin/partnerconsole ]] || {
  echo 'Refusing to replace an existing launcher.' >&2; exit 1;
}
test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
app="$(mktemp -d /srv/pc-credentials.XXXXXXXX)"
trap 'rm -f /usr/local/bin/partnerconsole; rm -rf -- "$app"' EXIT
cp -r "$source_tree/backend" "$source_tree/scripts" "$app/"
cp "$source_tree"/{partnerconsole,install,start,stop,restart}.sh "$app/"
mkdir -p "$app/.release" "$app/config" "$app/backend/data"
touch "$app/.release/current"
python3 -m venv "$app/backend/.venv"
python="$app/backend/.venv/bin/python"
"$python" -m pip install -q -e "$app/backend[dev]" pexpect
(
  export PARTNERCONSOLE_LIBRARY=1
  APP_ROOT="$app"
  # shellcheck disable=SC1091
  source "$app/partnerconsole.sh"
  install_launcher
)
"$python" "$test_dir/credentials-tty.py" "$app" "$mode"
