#!/usr/bin/env bash
# Requires a disposable Linux guest, root, ppv1, runuser, ss, and Python.
set -euo pipefail
umask 077
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
baseline="${1:-}"
baseline_common="${2:-}"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux && "$EUID" == 0 ]] || fail 'requires disposable Linux as root'
uid="$(id -u ppv1)"
[[ "$uid" != 0 ]] || fail 'requires a non-root service account'
[[ -z "$(ss -H -lnt '( sport = :8741 or sport = :3741 )')" ]] || fail 'test ports already occupied'

# shellcheck disable=SC1091
PARTNERCONSOLE_LIBRARY=1 source "$repo/partnerconsole.sh"
mkdir -p /srv
work="$(mktemp -d /srv/pc-service-check.XXXXXXXX)"
chmod 755 "$work"
export PARTNERCONSOLE_ROOT="$work/install/"
resolve_root
[[ "$APP_ROOT" == "$work/install" ]] || fail 'custom install root'
PP_ROOT="$APP_ROOT"
foreign=''
cleanup() {
  local result=$? log_file
  if [[ "$result" != 0 ]]; then
    ss -H -lntp '( sport = :8741 or sport = :3741 )' >&2 || true
    for log_file in "$PP_ROOT/logs/api.log" "$PP_ROOT/logs/web.log"; do
      if [[ -f "$log_file" ]]; then tail -n 12 "$log_file" >&2; fi
    done
    for identity in "$PP_ROOT/runtime/pids/"*.pid; do
      if [[ -f "$identity" ]]; then ps -p "$(<"$identity")" -o pid,user,stat,args >&2 || true; fi
    done
  fi
  if [[ -n "$foreign" ]]; then kill "$foreign" 2>/dev/null || true; wait "$foreign" 2>/dev/null || true; fi
  if [[ -f "$PP_ROOT/stop.sh" ]]; then runuser -u ppv1 -- bash "$PP_ROOT/stop.sh" >/dev/null 2>&1 || true; fi
  rm -rf -- "$work"
  exit "$result"
}
trap cleanup EXIT
mkdir -p "$PP_ROOT"/{scripts,config,.next,backend/.venv/bin,node_modules/.bin}
cp "$repo"/{partnerconsole,start,stop,restart,status,install}.sh "$PP_ROOT/"
cp "$repo/scripts"/{common,backup,bootstrap,domain}.sh "$PP_ROOT/scripts/"
chmod -R u=rwX,go=rX "$PP_ROOT"
for folder in runtime logs backups data; do
  install -d -o ppv1 -m700 "$PP_ROOT/$folder"
done
printf 'CORS_ORIGINS=http://127.0.0.1:3741\n' > "$PP_ROOT/config/api.env"
printf 'NEXT_PUBLIC_API_BASE=http://127.0.0.1:8741\n' > "$PP_ROOT/config/web.env"
printf 'fixture\n' > "$PP_ROOT/.next/BUILD_ID"
chgrp ppv1 "$PP_ROOT/config/"*.env
chmod 640 "$PP_ROOT/config/"*.env
chmod 644 "$PP_ROOT/.next/BUILD_ID"

cat > "$PP_ROOT/listener.py" <<'PY'
import os
import sys
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer

role = sys.argv[1]
origin = os.environ["CORS_ORIGINS"]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if role == "api":
            body = '{"ok":true}'
        elif self.path == "/":
            body = '<h1>ورود</h1><script src="/_next/static/common.js"></script><script src="/_next/static/page.js"></script>'
        else:
            body = f'let api="{origin}";'
        code = 400 if self.path == "/_next/static/page.js" and origin == "https://fail-asset.example.test" else 200
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_OPTIONS(self):
        allowed = self.headers.get("Origin") == origin
        self.send_response(200 if allowed else 400)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

class FixtureServer(TCPServer):
    allow_reuse_address = True

FixtureServer(("127.0.0.1", 8741 if role == "api" else 3741), Handler).serve_forever()
PY
cat > "$PP_ROOT/backend/.venv/bin/python" <<'EOF'
#!/bin/bash
if [[ "$1" == -c ]]; then printf '%s\n' "$PWD/backend/.venv"; else exec python3 "$PWD/listener.py" api; fi
EOF
cat > "$PP_ROOT/node_modules/.bin/next" <<'EOF'
#!/bin/bash
exec python3 "$PWD/listener.py" web
EOF
chmod 755 "$PP_ROOT/listener.py" "$PP_ROOT/backend/.venv/bin/python" "$PP_ROOT/node_modules/.bin/next"
export PYTHONDONTWRITEBYTECODE=1
# shellcheck disable=SC1091
source "$PP_ROOT/scripts/common.sh"

expect_state() {
  local result=0
  app_running || result=$?
  [[ "$result" == "$1" ]] || fail "$2: expected state $1 but got $result"
  printf 'ok %s\n' "$2"
}
single() {
  # shellcheck disable=SC2016
  runuser -u ppv1 -- bash -c '
    set -euo pipefail
    PP_ROOT="$1"; cd "$PP_ROOT"
    source scripts/common.sh
    pp_load_env_file config/api.env
    pp_launch_service "$2" python3 "$PP_ROOT/listener.py" "$2"
  ' bash "$PP_ROOT" "$1"
  local port=8741
  [[ "$1" == api ]] || port=3741
  curl -fsS --retry 12 --retry-connrefused --retry-delay 1 "http://127.0.0.1:$port/" >/dev/null
}

expect_state 1 'both services stopped'
# A slow setsid keeps the child in the caller's process group for a moment.
install -d -m 755 "$work/slow-bin"
printf '#!/bin/sh\nsleep 0.5\nexec %s "$@"\n' "$(command -v setsid)" > "$work/slow-bin/setsid"
chmod 755 "$work/slow-bin/setsid"
# shellcheck disable=SC2016
runuser -u ppv1 -- env PATH="$work/slow-bin:$PATH" bash -c '
  set -euo pipefail
  PP_ROOT="$1"; cd "$PP_ROOT"
  source scripts/common.sh
  pp_load_env_file config/api.env
  pp_launch_service api python3 "$PP_ROOT/listener.py" api
' bash "$PP_ROOT"
# shellcheck disable=SC2218
curl -fsS --retry 12 --retry-connrefused --retry-delay 1 http://127.0.0.1:8741/ >/dev/null
expect_state 0 'identity is recorded after a delayed setsid'
stop_app
expect_state 1 'the delayed setsid launch stopped cleanly'
single api
expect_state 0 'root recognizes API only under ppv1'
if [[ -n "$baseline" ]]; then
  (
    # shellcheck disable=SC1090
    PARTNERCONSOLE_LIBRARY=1 source "$baseline"
    if app_running; then fail 'RC5 did not reproduce the root detector failure'; fi
  )
  echo 'ok RC5 root detector failed against the same running API fixture'
fi
# shellcheck disable=SC2016
runuser -u ppv1 -- bash -c 'PP_ROOT="$1"; source "$PP_ROOT/scripts/common.sh"; pp_leader_matches api' bash "$PP_ROOT"
echo 'ok service account recognizes its process'
stop_app
single web
expect_state 0 'root recognizes Web only under ppv1'
start_app
expect_state 0 'root recognizes both services'

api_identity="$PP_ROOT/runtime/pids/api.id"
cp -p "$api_identity" "$work/identity"
pp_load_service_identity api
legitimate="$PP_ID_PID"
for changed in starttime uid pgid; do
  sed -E "s/^$changed=.*/$changed=1/" "$work/identity" > "$api_identity"
  expect_state 2 "root rejects inconsistent $changed"
done
printf 'pid=invalid\n' > "$api_identity"
expect_state 2 'malformed identity is unsafe'
cp -p "$work/identity" "$api_identity"
mkdir -p "$work/other/runtime/pids"
cp "$api_identity" "$work/other/runtime/pids/api.id"
if PP_ROOT="$work/other" pp_leader_matches api "$uid"; then fail 'wrong installation root accepted'; fi
echo 'ok wrong installation root rejected'

run_root_script() { echo backup >> "$work/events"; }
if (backup_app) >"$work/backup-error" 2>&1; then fail 'running Backup allowed'; fi
grep -Fq 'Stop the app' "$work/backup-error"
printf 'pid=invalid\n' > "$api_identity"
if (backup_app) >"$work/backup-error" 2>&1; then fail 'unknown Backup allowed'; fi
grep -Fq 'Backup refused' "$work/backup-error"
if stop_app; then fail 'unsafe Stop was allowed'; fi
kill -0 "$legitimate" || fail 'valid process signalled during unsafe Stop'
cp -p "$work/identity" "$api_identity"
stop_app
backup_app
[[ "$(cat "$work/events")" == backup ]] || fail 'stopped Backup did not reach its entry point'
echo 'ok Backup guards for running, unknown and stopped'

( cd "$PP_ROOT"; export CORS_ORIGINS=https://other.example.test; exec setsid python3 "$PP_ROOT/listener.py" api ) &
foreign=$!
# shellcheck disable=SC2218
curl -fsS --retry 12 --retry-connrefused --retry-delay 1 http://127.0.0.1:8741/api/health >/dev/null
expect_state 2 'unrecorded foreign listener'
printf 'pid=%s\nstarttime=%s\nuid=%s\npgid=%s\n' "$foreign" "$(pp_stat_field "$foreign" 20)" "$(id -u)" "$foreign" > "$api_identity"
printf '%s\n' "$foreign" > "$PP_ROOT/runtime/pids/api.pid"
chown ppv1 "$api_identity" "$PP_ROOT/runtime/pids/api.pid"
expect_state 2 'valid-looking records for a root listener are rejected'
if restart_app; then fail 'unsafe Restart was allowed'; fi
kill -0 "$foreign" || fail 'unrelated root process was signalled'
kill "$foreign"; wait "$foreign" || true; foreign=''
pp_clear_service_identity api

# Exercise the actual Update entry point using a verified-tree fixture and
# real restart. Installation/SQL/package work is outside this process test.
fetch_release_package() {
  local incoming="$APP_ROOT/.release/incoming"
  mkdir -p "$incoming/scripts"
  cp "$repo/scripts/common.sh" "$incoming/scripts/"
}
release_already_current() { return 1; }
reject_local_changes() { :; }
ensure_user() { :; }
install_deps() { :; }
deploy_fetched() {
  echo deploy >> "$work/update-events"
  cp "$PP_ROOT/.release/incoming/scripts/common.sh" "$PP_ROOT/scripts/common.sh"
}
install_launcher() { :; }
printf 'invalid\n' > "$api_identity"
if (update_app) > "$work/update-error" 2>&1; then fail 'unknown Update was allowed'; fi
grep -Fq 'Update stopped' "$work/update-error"
[[ ! -e "$work/update-events" ]] || fail 'unknown Update reached deployment'
echo 'ok Update refused ambiguous process identity before deployment'
rm -- "$api_identity"
update_app
expect_state 1 'Update kept both processes stopped'
start_app
pp_load_service_identity api
old="$PP_ID_PID"
printf 'CORS_ORIGINS=https://console.example.test\n' > "$PP_ROOT/config/api.env"
if [[ -n "$baseline_common" ]]; then cp "$baseline_common" "$PP_ROOT/scripts/common.sh"; fi
update_app
pp_load_service_identity api
[[ "$PP_ID_PID" != "$old" ]] || fail 'running Update did not restart the API'
cors_status="$(curl -fsS -o /dev/null -w '%{http_code}' -X OPTIONS -H 'Origin: https://console.example.test' http://127.0.0.1:8741/api/admin/auth/login)"
[[ "$cors_status" == 200 ]] || fail 'new environment did not reach the API'
expect_state 0 'Update restarted the real processes with the new environment'

# Domain tests replace certificate issuance and the Nginx transport. The
# installation files, processes, official restart, HTTP and CORS are real.
# shellcheck disable=SC1091
source "$PP_ROOT/scripts/domain.sh"
PP_NGINX_SITE="$work/nginx/sites-available/partnerconsole.conf"
PP_NGINX_LINK="$work/nginx/sites-enabled/partnerconsole.conf"
PP_ACME_ROOT="$work/acme"
PP_CERT_FULLCHAIN="$work/cert.pem"
PP_CERT_PRIVKEY="$work/privkey.pem"
pp_domain_same_https() { return 1; }
pp_domain_nginx_test() { :; }
pp_domain_nginx_reload() { :; }
pp_domain_acme_probe() { :; }
pp_domain_issue_cert() { :; }
pp_domain_cert_files_present() { :; }
pp_domain_build_web() { printf 'new-build\n' > "$PP_ROOT/.next/BUILD_ID"; }
pp_domain_bundle_ok() { :; }
pp_domain_install_renew_hook() { :; }
pp_domain_enable_timer() { :; }
systemctl() { [[ "$*" == 'is-active --quiet nginx' ]] && return 3; return 1; }
pp_domain_fetch() {
  local path="$2" port=3741 body status
  if [[ "$path" == /.well-known/acme-challenge/ ]]; then printf '%s\n' 404 -; return; fi
  [[ "$path" != /api/* ]] || port=8741
  body="$(mktemp)"
  status="$(command curl -sS --connect-timeout 2 -o "$body" -w '%{http_code}' "http://127.0.0.1:$port$path")" || {
    rm -f "$body"
    return 1
  }
  printf '%s\n' "$status" -
  cat "$body"
  rm -f "$body"
}
curl() {
  local -a args=()
  while (($#)); do
    case "$1" in
      --resolve) shift ;;
      https://*/api/admin/auth/login) args+=(http://127.0.0.1:8741/api/admin/auth/login) ;;
      *) args+=("$1") ;;
    esac
    shift
  done
  command curl "${args[@]}"
}

pp_load_service_identity api
old="$PP_ID_PID"
pp_domain_activate new.example.test
pp_load_service_identity api
[[ "$PP_ID_PID" != "$old" ]] || fail 'Domain did not restart the running app'
pp_domain_cors_ok new.example.test || fail 'Domain did not load its CORS origin'
echo 'ok Domain restarted the app and validated JavaScript and CORS'
if pp_domain_activate fail-asset.example.test; then fail 'Domain accepted HTTP 400 after restart'; fi
pp_domain_cors_ok new.example.test || fail 'rollback left the changed CORS origin in the active process'
pp_domain_https_probe new.example.test || fail 'rollback left the previous runtime unhealthy'
echo 'ok Domain rejected the asset failure and restored the running previous environment'

cp -p "$api_identity" "$work/identity"
printf 'invalid\n' > "$api_identity"
if pp_domain_activate unknown.example.test; then fail 'Domain accepted unknown process identity'; fi
grep -Fxq 'CORS_ORIGINS=https://new.example.test' "$PP_ROOT/config/api.env" || fail 'unknown Domain mutated settings'
cp -p "$work/identity" "$api_identity"

(
  wait_web() {
    echo check >> "$work/restart-health"
    [[ "$(wc -l < "$work/restart-health")" == 2 ]]
  }
  if pp_domain_activate restart-fails.example.test; then fail 'Domain accepted a failed Restart health check'; fi
  [[ "$(wc -l < "$work/restart-health")" == 2 ]] || fail 'the previous runtime was not restarted on rollback'
)
pp_domain_cors_ok new.example.test || fail 'failed Restart rollback lost the previous origin'
stop_app
expect_state 1 'official Stop removed only the installation processes'
pp_domain_activate stopped.example.test
expect_state 1 'Domain kept a stopped installation stopped'
