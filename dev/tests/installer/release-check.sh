#!/usr/bin/env bash
# Isolated checks for the Partner Console installer. No server and no real database.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export PARTNERCONSOLE_LIBRARY=1
# shellcheck disable=SC1091
source "$repo/partnerconsole.sh"
# shellcheck disable=SC1091
source "$repo/stop.sh"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok %s\n' "$*"; }

nonroot_can_write_mode 755 && fail "mode 755 was treated as writable"
nonroot_can_write_mode 644 && fail "mode 644 was treated as writable"
nonroot_can_write_mode 775 || fail "mode 775 was treated as safe"
nonroot_can_write_mode 666 || fail "mode 666 was treated as safe"
ok "permission bits"

( APP_ROOT=/srv/partnerconsole; validate_root ) || fail "the default install path was rejected"
if ( APP_ROOT=/etc; validate_root ); then
  fail "a system path was accepted"
fi
if ( APP_ROOT=/tmp/partnerconsole; validate_root ); then
  fail "a temporary path was accepted"
fi
ok "install path"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_SYSTEM=/dev/null
origin="$work/origin"
view="$work/tag-view"
install="$work/install"
mkdir -p "$origin/backend" "$view" "$install"

git -C "$origin" init -q
printf 'version = "%s"\n' "$APP_VERSION" > "$origin/backend/pyproject.toml"
printf 'RELEASE_TREE_SHA256=""\nkeep=tag\n' > "$origin/partnerconsole.sh"
printf 'old\n' > "$origin/app.txt"
git -C "$origin" add .
git -C "$origin" -c user.email=test@example.com -c user.name=test commit -qm "release"
git -C "$origin" tag "$RELEASE_TAG"
printf 'new\n' > "$origin/app.txt"
git -C "$origin" -c user.email=test@example.com -c user.name=test commit -qam "main"

git -C "$view" init -q
git -C "$view" -c protocol.file.allow=always fetch --depth 1 "$origin" tag "$RELEASE_TAG"
git -C "$view" checkout -q --detach FETCH_HEAD
RELEASE_TREE_SHA256="$(tree_sha256 "$view")"
[[ "$RELEASE_TREE_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "tree hash was not created"
printf 'changed\n' >> "$view/app.txt"
[[ "$(tree_sha256 "$view")" != "$RELEASE_TREE_SHA256" ]] || fail "a changed file kept the same hash"
git -C "$view" checkout -q -- app.txt
ok "release hash"

APP_ROOT="$install"
fetch_git_tag "$origin" "$APP_ROOT/.release/incoming" "$RELEASE_TAG" >/dev/null
grep -q '^old$' "$APP_ROOT/.release/incoming/app.txt" || fail "the tag checkout was not used"
if grep -q '^new$' "$APP_ROOT/.release/incoming/app.txt"; then
  fail "main was checked out"
fi
ok "fetch tag"

saved="$RELEASE_TREE_SHA256"
RELEASE_TREE_SHA256="0000000000000000000000000000000000000000000000000000000000000000"
if ( fetch_git_tag "$origin" "$APP_ROOT/.release/incoming" "$RELEASE_TAG" >/dev/null ); then
  fail "a bad integrity pin was accepted"
fi
RELEASE_TREE_SHA256="$saved"
ok "bad pin rejected"

mkdir -p "$install/config" "$install/data"
printf 'secret-marker\n' > "$install/config/api.env"
printf 'userdata\n' > "$install/data/keep"
printf 'previous\n' > "$install/app.txt"
mkdir -p "$APP_ROOT/.release/incoming/config"
printf 'LEAK\n' > "$APP_ROOT/.release/incoming/config/api.env"
snapshot_release "$APP_ROOT/.release/previous"
apply_incoming
grep -q 'secret-marker' "$install/config/api.env" || fail "configuration was replaced"
if grep -q 'LEAK' "$install/config/api.env"; then
  fail "incoming configuration was copied"
fi
grep -q 'userdata' "$install/data/keep" || fail "user data was removed"
grep -q '^old$' "$install/app.txt" || fail "release file was not installed"
restore_release
grep -q '^previous$' "$install/app.txt" || fail "rollback did not restore the code"
grep -q 'secret-marker' "$install/config/api.env" || fail "rollback changed configuration"
grep -q 'userdata' "$install/data/keep" || fail "rollback changed user data"
ok "snapshot rollback"

fake="$work/bin"
mkdir -p "$fake"
cat > "$fake/mysql" <<'EOF'
#!/usr/bin/env bash
printf '3\n'
EOF
chmod 755 "$fake/mysql"
if ( PATH="$fake:$PATH" assert_database_empty ); then
  fail "a database with tables was accepted"
fi
ok "existing database refused"

sql_out="$work/create.sql"
cat > "$fake/mysql" <<'EOF'
#!/usr/bin/env bash
if [[ "${2:-}" == "-N" ]]; then
  printf '0\n'
  exit 0
fi
cat > "$SQL_OUT"
EOF
cat > "$fake/mysql-mock.py" <<'PY'
import os
import sys
from pathlib import Path
if "-N" in sys.argv[1:]:
    print("0")
else:
    Path(os.environ["SQL_OUT"]).write_bytes(sys.stdin.buffer.read())
PY
cat > "$fake/mysql.cmd" <<'EOF'
@echo off
python "%~dp0mysql-mock.py" %*
EOF
export SQL_OUT
if command -v cygpath >/dev/null 2>&1; then
  SQL_OUT="$(cygpath -w "$sql_out")"
else
  SQL_OUT="$sql_out"
fi
DB_NAME=partnerconsole
DB_USER=partnerconsole
PATH="$fake:$PATH" create_database "secret-pass"
pp_python - "$sql_out" <<'PY' || fail "database SQL did not quote the identifier safely"
import sys
from pathlib import Path
data = Path(sys.argv[1]).read_bytes()
ok = b"CREATE DATABASE IF NOT EXISTS `partnerconsole`" in data and b"\\`" not in data
ok = ok and b"IDENTIFIED BY 'secret-pass'" in data
raise SystemExit(0 if ok else 1)
PY
if ( DB_NAME='bad-name'; assert_mysql_ident "$DB_NAME" ); then
  fail "an unsafe database name was accepted"
fi
ok "database sql"

mkdir -p "$install/.release"
printf 'previous\n' > "$install/app.txt"
printf 'app.txt\n' > "$install/.release/managed.txt"
record_applied_files
printf 'local-edit\n' >> "$install/app.txt"
if ( reject_local_changes deadbeef deadbeef ); then
  fail "a local change was accepted"
fi
grep -q 'local-edit' "$install/app.txt" || fail "a local change was deleted"
printf 'previous\n' > "$install/app.txt"
record_applied_files
reject_local_changes deadbeef deadbeef || fail "a clean tree was rejected"
ok "local change stops update"

ss() {
  printf '%s\n' 'LISTEN 0 5 127.0.0.1:8741 0.0.0.0:*'
}
pp_port_listening 8741 || fail "an occupied port was not seen"
if pp_port_listening 3741; then
  fail "a free port was reported busy"
fi
unset -f ss
ok "occupied port"

proc="$work/proc-home"
mkdir -p "$proc"
cat > "$proc/hold.sh" <<'EOF'
#!/usr/bin/env bash
sleep 30
EOF
chmod 755 "$proc/hold.sh"
(
  cd "$proc" || exit 1
  exec ./hold.sh
) &
sleep_pid=$!
sleep 0.3
export PP_ROOT="$proc"
if pp_process_belongs_to_install "$sleep_pid"; then
  ok "process in the install"
else
  kill "$sleep_pid" 2>/dev/null || true
  fail "a process started in the install was ignored"
fi
export PP_ROOT="$work/other-install"
if pp_process_belongs_to_install "$sleep_pid"; then
  kill "$sleep_pid" 2>/dev/null || true
  fail "a process from another directory was accepted"
fi
kill "$sleep_pid" 2>/dev/null || true
wait "$sleep_pid" 2>/dev/null || true
ok "process scope"

mysql_listener_is_safe "127.0.0.1:3306" || fail "IPv4 loopback MySQL was rejected"
mysql_listener_is_safe "[::1]:3306" || fail "IPv6 loopback MySQL was rejected"
if mysql_listener_is_safe "127.0.0.1:33060" || mysql_listener_is_safe "[::1]:33060"; then
  fail "MySQL X on loopback was accepted"
fi
if mysql_listener_is_safe "*:3306" || mysql_listener_is_safe "0.0.0.0:3306" || mysql_listener_is_safe "[::]:3306"; then
  fail "a wildcard MySQL listener was accepted"
fi
if mysql_listener_is_safe "[::]:33060" || mysql_listener_is_safe "65.109.208.176:3306"; then
  fail "a public MySQL listener was accepted"
fi
ok "mysql listeners"

mysql_config_modes_ok 644 0 755 0 755 0 || fail "root mode 644 with traversable parents was rejected"
if mysql_config_modes_ok 600 0 755 0; then
  fail "a root-only MySQL config was accepted"
fi
if mysql_config_modes_ok 644 0 700 0; then
  fail "a MySQL config behind a private directory was accepted"
fi
if mysql_config_modes_ok 644 0 777 0 || mysql_config_modes_ok 666 0 755 0; then
  fail "a writable MySQL config was accepted"
fi
ok "mysql config mode"

guard="$work/mysql-guard.nft"
mysql_guard_nft > "$guard"
mysql_guard_preserves_ssh "$guard" || fail "the MySQL guard can change SSH or flush the firewall"
if grep -q 'systemctl enable nftables' "$repo/partnerconsole.sh" || grep -q 'partnerconsole-mysql-guard.service' "$repo/partnerconsole.sh"; then
  fail "the MySQL network guard is installed as a boot service"
fi
ok "mysql guard keeps ssh"

(
  calls=""
  runtime_tries=0
  PATH="/usr/bin:/bin"
  mysql_version_ok() { [[ "${fake_mysql_ok:-}" == "1" ]]; }
  mysql_network_guard_up() { calls="${calls} guard"; }
  write_mysql_local_config() { calls="${calls} config"; }
  install_mysql_packages() { calls="${calls} packages"; fake_mysql_ok=1; }
  assert_mysql_config_access() { calls="${calls} access"; }
  systemctl() { calls="${calls} systemctl"; }
  wait_mysql() { return 0; }
  mysql_runtime_is_safe() { return 0; }
  fake_mysql_ok=1
  ensure_mysql
  [[ "$calls" == " guard config access systemctl" ]] || fail "an existing local MySQL was reinstalled: ${calls}"
  calls=""
  fake_mysql_ok=""
  mysql_runtime_is_safe() {
    runtime_tries=$((runtime_tries + 1))
    [[ "$runtime_tries" -ge 2 ]]
  }
  ensure_mysql
  [[ "$calls" == *"guard config packages"* ]] || fail "MySQL packages started before the guard: ${calls}"
  [[ "$calls" == *"packages access systemctl systemctl" ]] || fail "an unsafe startup was not restarted: ${calls}"
)
ok "mysql install order"

mkdir -p "$install/backend/.venv/bin" "$install/.release/previous"
ln -s /usr/bin/bash "$install/backend/.venv/bin/python"
snapshot_release "$APP_ROOT/.release/previous" || fail "a virtualenv symlink stopped the snapshot"
if [[ -e "$install/.release/previous/backend/.venv/bin/python" ]]; then
  fail "the virtualenv was copied into the source snapshot"
fi
mkdir -p "$work/outside"
printf 'outside\n' > "$work/outside/marker"
ln -s ../outside/marker "$install/escape.link" || fail "could not create a symlink for the snapshot test"
if pp_python - "$install/escape.link" <<'PY'
import sys
from pathlib import Path
raise SystemExit(0 if Path(sys.argv[1]).is_symlink() else 2)
PY
then
  if snapshot_release "$APP_ROOT/.release/previous"; then
    fail "a symlink that leaves the install was copied"
  fi
  echo "ok symlink escape rejected"
else
  echo "SYMLINK_ESCAPE_NOT_TESTED"
fi
rm -f "$install/escape.link"
ok "snapshot skips the virtualenv"

before="$(stat -c '%u:%g:%a' "$install")"
printf 'keep-owner\n' > "$install/owner-marker"
ensure_install_directory
after="$(stat -c '%u:%g:%a' "$install")"
[[ "$before" == "$after" ]] || fail "an existing install directory was reowned"
grep -q 'keep-owner' "$install/owner-marker" || fail "an existing install directory was replaced"
ok "existing install ownership"

printf 'tag=%s\ncommit=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ntree=pin\n' "$RELEASE_TAG" > "$install/.release/current"
release_already_current aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa || fail "an unchanged release was not a no-op"
printf 'edited\n' >> "$install/app.txt"
if release_already_current aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; then
  fail "a locally edited release was treated as current"
fi
printf 'previous\n' > "$install/app.txt"
snapshot_release "$APP_ROOT/.release/previous" || fail "could not snapshot the previous source"
ok "same commit no-op"

(
  calls="$work/rollback-calls"
  : > "$calls"
  apply_permissions() { echo perm >> "$calls"; chmod 750 "$APP_ROOT"; }
  run_root_script() { echo install >> "$calls"; printf 'restored-deps\n' > "$APP_ROOT/install-marker"; }
  assert_next_module() { return 0; }
  mkdir -p "$APP_ROOT/node_modules/next" "$APP_ROOT/backend/.venv" "$APP_ROOT/.next"
  printf 'changed-during-update\n' > "$APP_ROOT/app.txt"
  chmod 700 "$APP_ROOT"
  probe="$work/mode-probe"
  mkdir -p "$probe"
  chmod 750 "$probe"
  expected_mode="$(stat -c '%a' "$probe")"
  rollback_failed_release || fail "rollback did not rebuild the previous release"
  [[ ! -d "$APP_ROOT/node_modules" ]] || fail "node_modules survived the rollback"
  [[ ! -d "$APP_ROOT/backend/.venv" ]] || fail "the virtualenv survived the rollback"
  grep -q '^previous$' "$APP_ROOT/app.txt" || fail "rollback did not restore the source file"
  [[ "$(stat -c '%a' "$APP_ROOT")" == "$expected_mode" ]] || fail "rollback did not restore directory access"
  grep -q install "$calls" || fail "rollback did not reinstall dependencies"
)
ok "failed update restores code and access"

service="$work/service"
mkdir -p "$service"
if ! command -v setsid >/dev/null 2>&1; then
  setsid() { exec "$@"; }
fi
PP_ROOT="$service"
pp_prepare_runtime
(
  cd "$service" || exit 1
  pp_launch_service web /usr/bin/bash -c 'exec -a "next-server (v15.5.29)" /usr/bin/sleep 30'
) || fail "the renamed process did not start"
pp_load_service_identity web || fail "the renamed process was not recorded"
if pp_process_belongs_to_install "$PP_ID_PID"; then
  pp_stop_service web >/dev/null 2>&1 || true
  fail "a renamed process was accepted from its command line"
fi
pp_leader_matches web || fail "a renamed Next.js process was not recognized"
pp_stop_service web >/dev/null || fail "stop did not stop the renamed process"
pp_stop_service web >/dev/null || fail "a second stop failed"
if pp_leader_matches web; then
  fail "stop left the service recorded as running"
fi
ok "renamed process stop"

(
  cd "$service" || exit 1
  exec -a "next-server (v15.5.29)" /usr/bin/sleep 30
) &
foreign=$!
sleep 0.2
printf 'pid=%s\nstarttime=1\nuid=%s\npgid=1\n' "$foreign" "$(id -u)" > "$service/runtime/pids/api.id"
printf '%s\n' "$foreign" > "$service/runtime/pids/api.pid"
chmod 600 "$service/runtime/pids/api.id" "$service/runtime/pids/api.pid"
pp_stop_service api >/dev/null || fail "stop failed on a reused pid"
kill -0 "$foreign" 2>/dev/null || fail "stop killed a reused pid"
kill "$foreign" 2>/dev/null || true
wait "$foreign" 2>/dev/null || true
ok "reused pid is left running"

other="$work/other-service"
mkdir -p "$other"
(
  cd "$other" || exit 1
  exec /usr/bin/sleep 30
) &
other_pid=$!
sleep 0.2
PP_ROOT="$service"
pp_write_service_identity web "$other_pid" || fail "the identity file could not be written"
if pp_leader_matches web; then
  pp_stop_service web >/dev/null 2>&1 || true
  kill "$other_pid" 2>/dev/null || true
  fail "a process from another directory matched this install"
fi
pp_stop_service web >/dev/null || fail "stop failed for another install's pid"
kill -0 "$other_pid" 2>/dev/null || fail "stop killed a process from another install"
kill "$other_pid" 2>/dev/null || true
wait "$other_pid" 2>/dev/null || true
ok "other install is not recorded"

(
  attempts=0
  pp_leader_matches() { return 0; }
  pp_port_owned_by_service() { return 0; }
  pp_http_matches() { attempts=$((attempts + 1)); [[ "$attempts" -ge 3 ]]; }
  sleep() { :; }
  pp_wait_for_ready api 8741 "http://127.0.0.1:8741/api/health" api || exit 1
  [[ "$attempts" -eq 3 ]] || exit 1
) || fail "start did not wait for a delayed service"
ok "start waits for readiness"

pp_test_curl() {
  local target="" out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o) out="$2"; shift 2 ;;
      -H|-w|--max-time) shift 2 ;;
      -sS) shift ;;
      http*) target="$1"; shift ;;
      *) shift ;;
    esac
  done
  if [[ "$target" == *"/_next/static/"* ]]; then
    printf '200'
    return 0
  fi
  if [[ "$target" == *8741* ]]; then
    printf '%s' '{"ok":true}' > "$out"
    printf '200'
    return 0
  fi
  if [[ "$target" == *error* ]]; then
    printf '%s' 'Internal Server Error' > "$out"
    printf '200'
    return 0
  fi
  printf '%s' '<html>ورود<script src="/_next/static/chunks/app.js"></script></html>' > "$out"
  printf '200'
}
(
  curl() { pp_test_curl "$@"; }
  pp_http_matches "http://127.0.0.1:8741/api/health" api || exit 1
  pp_http_matches "http://127.0.0.1:3741/" web || exit 1
  pp_http_matches "http://127.0.0.1:3741/error" web && exit 1
  exit 0
) || fail "web health accepted a broken page or rejected the login page"
ok "web health rejects an error page"

backup_root="$work/backup-root"
mkdir -p "$backup_root/config" "$backup_root/data" "$backup_root/logs" "$backup_root/backups" "$backup_root/runtime/tmp" "$backup_root/runtime/cache/pip" "$backup_root/runtime/cache/npm" "$backup_root/runtime/cache/xdg" "$backup_root/runtime/cache/pyc" "$backup_root/runtime/pids"
printf 'SECRET_KEY=not-a-real-secret\n' > "$backup_root/config/api.env"
chmod 777 "$backup_root/config" "$backup_root/config/api.env" "$backup_root/data"
(
  PP_ROOT="$backup_root"
  id() { [[ "${1:-}" == "-u" ]] && printf '0\n'; }
  pp_prepare_runtime
  mkdir -p "$work/config-probe" "$work/data-probe"
  printf 'x\n' > "$work/env-probe"
  chmod 750 "$work/config-probe"
  chmod 640 "$work/env-probe"
  chmod 700 "$work/data-probe"
  got_config="$(stat -c '%a' "$backup_root/config")"
  got_env="$(stat -c '%a' "$backup_root/config/api.env")"
  got_data="$(stat -c '%a' "$backup_root/data")"
  [[ "$got_config" == "$(stat -c '%a' "$work/config-probe")" ]] || { printf 'config %s\n' "$got_config"; exit 1; }
  [[ "$got_env" == "$(stat -c '%a' "$work/env-probe")" ]] || { printf 'env %s\n' "$got_env"; exit 1; }
  [[ "$got_data" == "$(stat -c '%a' "$work/data-probe")" ]] || { printf 'data %s\n' "$got_data"; exit 1; }
  if [[ "$(stat -c '%a' "$work/config-probe")" == "$(stat -c '%a' "$work/data-probe")" ]]; then
    echo "CONFIG_MODE_BITS_NOT_TESTED"
  elif [[ "$got_config" == "$got_data" ]]; then
    exit 1
  fi
) || fail "backup tightened config so the app user cannot read it"
grep -F -q 'chmod 750 "$PP_ROOT/config"' "$repo/scripts/common.sh" || fail "config is not left group-readable"
if grep -F -q 'chmod 700 "$PP_ROOT/config"' "$repo/scripts/common.sh"; then
  fail "config is locked away from the app user"
fi
ok "backup keeps config readable"

dump_root="$work/dump-root"
mkdir -p "$dump_root/config" "$dump_root/backend/.venv/bin" "$dump_root/backups" "$dump_root/runtime/tmp" "$dump_root/logs" "$dump_root/data" "$dump_root/runtime/pids" "$dump_root/runtime/cache/pip" "$dump_root/runtime/cache/npm" "$dump_root/runtime/cache/xdg" "$dump_root/runtime/cache/pyc"
printf 'DATABASE_URL=mysql+pymysql://partnerconsole:backup-test@127.0.0.1:3306/partnerconsole\n' > "$dump_root/config/api.env"
cat > "$dump_root/backend/.venv/bin/python" <<'EOF'
#!/usr/bin/env bash
exec python "$@"
EOF
chmod 755 "$dump_root/backend/.venv/bin/python"
mkdir -p "$work/fakebin"
cat > "$work/fakebin/mysqldump" <<'EOF'
#!/usr/bin/env bash
out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --result-file=*) out="${1#--result-file=}" ;;
    --result-file) shift; out="${1:-}" ;;
  esac
  shift || true
done
[[ -n "$out" ]] || exit 1
printf 'CREATE TABLE backup_probe(id int);\n' > "$out"
EOF
chmod 755 "$work/fakebin/mysqldump"
PP_ROOT="$dump_root"
pp_prepare_runtime
(
  cd "$dump_root" || exit 1
  pp_launch_service api /usr/bin/sleep 30
) || fail "could not start a process for the backup guard"
if PP_ROOT="$dump_root" PATH="$work/fakebin:$PATH" bash "$repo/scripts/backup.sh" >"$work/backup-refuse.out" 2>&1; then
  pp_stop_service api >/dev/null 2>&1 || true
  fail "backup ran while this install was still running"
fi
grep -q 'Stop the application before backup.' "$work/backup-refuse.out" || fail "backup failed before it recognized the running install"
pp_stop_service api >/dev/null || fail "could not stop the install before backup"
PP_ROOT="$dump_root" PATH="$work/fakebin:$PATH" bash "$repo/scripts/backup.sh" >/dev/null || fail "backup did not write a dump"
dump_file="$(find "$dump_root/backups" -name 'partnerconsole-*.sql' -type f | head -n 1)"
[[ -n "$dump_file" ]] || fail "backup did not create a sql file"
grep -q 'CREATE TABLE backup_probe' "$dump_file" || fail "backup dump was empty"
dump_mode="$(stat -c '%a' "$dump_file")"
[[ $((8#$dump_mode & 022)) -eq 0 ]] || fail "backup dump is not private"
ok "backup writes a private dump"

printf 'installer checks passed\n'
