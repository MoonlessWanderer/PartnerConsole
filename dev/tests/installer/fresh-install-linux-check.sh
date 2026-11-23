#!/usr/bin/env bash
# A private candidate origin on a disposable Ubuntu 24.04 host.
set -euo pipefail
umask 077
[[ $(uname -s) == Linux && "$EUID" == 0 && "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]]
# shellcheck disable=SC1091
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
origin="${1:?provide the candidate-only Git origin}"
expected_commit="${2:?provide the candidate commit}"
export PARTNERCONSOLE_ROOT=/srv/pc-fresh-check
[[ ! -e "$PARTNERCONSOLE_ROOT" && ! -e /usr/local/bin/partnerconsole ]]
[[ -z "$(ss -H -lnt '( sport = :8741 or sport = :3741 )')" ]]
export PARTNERCONSOLE_LIBRARY=1
# shellcheck disable=SC1091
source "$repo/partnerconsole.sh"
resolve_root
validate_root
[[ "$(git -c safe.directory="$origin" -C "$origin" rev-parse "refs/tags/$RELEASE_TAG^{commit}")" == "$expected_commit" ]]
[[ "$(git -c safe.directory="$repo" -C "$repo" rev-list --count HEAD)" == 1 ]]
OFFICIAL_REPO="$origin"
ask_origin() { printf '%s\n' http://127.0.0.1:3741 http://127.0.0.1:8741; }
ask_admin_password() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

work="$(mktemp -d)"
ssh_listener=
cleanup() {
  local status=$? log_file
  if [[ "$status" != 0 ]]; then
    for log_file in "$APP_ROOT/logs/api.log" "$APP_ROOT/logs/web.log"; do
      if [[ -f "$log_file" ]]; then tail -n 20 "$log_file"; fi
    done
  fi
  if [[ -x /usr/local/bin/partnerconsole ]]; then env -u PARTNERCONSOLE_LIBRARY /usr/local/bin/partnerconsole stop || true; fi
  if [[ -n "$ssh_listener" ]]; then kill "$ssh_listener" 2>/dev/null || true; fi
  rm -rf -- "$work"
}
trap cleanup EXIT

# Some runners lack an SSH listener. Use loopback to exercise the guard
# without exposing an additional port.
if [[ -z "$(ss -H -lnt 'sport = :22')" ]]; then
  python3 - <<'PYTHON' &
import socket
import time
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 22))
    listener.listen()
    time.sleep(1500)
PYTHON
  ssh_listener=$!
  for _ in $(seq 1 20); do
    if [[ -n "$(ss -H -lnt 'sport = :22')" ]]; then break; fi
    sleep 0.1
  done
fi

setup_app "$OFFICIAL_REPO"
grep -Fx "commit=$expected_commit" "$APP_ROOT/.release/current"
grep -Fx "tag=$RELEASE_TAG" "$APP_ROOT/.release/current"
grep -Fx "tree=$RELEASE_TREE_SHA256" "$APP_ROOT/.release/current"
cmp "$repo/partnerconsole.sh" "$APP_ROOT/partnerconsole.sh"
env -u PARTNERCONSOLE_LIBRARY /usr/local/bin/partnerconsole start
curl -fsS http://127.0.0.1:8741/api/health > "$work/health.json"
curl -fsS http://127.0.0.1:8741/openapi.json > "$work/openapi.json"
curl -fsS http://127.0.0.1:3741/ > "$work/login.html"
python3 - "$work" "$APP_VERSION" <<'PYTHON'
import json
import sys
from pathlib import Path
work = Path(sys.argv[1])
assert json.loads((work / "health.json").read_text()) == {"ok": True}
assert json.loads((work / "openapi.json").read_text())["info"]["version"] == sys.argv[2]
assert "<title>Partner Console</title>" in (work / "login.html").read_text()
PYTHON
printf 'Candidate %s installed and started on Ubuntu. API health, version and sign-in page passed.\n' "$expected_commit"
