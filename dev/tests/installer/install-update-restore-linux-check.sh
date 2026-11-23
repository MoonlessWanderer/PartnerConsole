#!/usr/bin/env bash
# Disposable Ubuntu 24.04 as root only, for example a GitHub-hosted runner.
# Uses the installer's own functions to install MySQL 8.4 and the published
# baseline fixture, runs the official Update to the release tag in
# RELEASE_ORIGIN, then runs the official
# Backup and Restore against fixture data. Prompts are answered by the test.
# Usage: PARTNERCONSOLE_DISPOSABLE_HOST=1 RELEASE_ORIGIN=REPO BROWSER_USER=USER bash THIS
set -euo pipefail
umask 077

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok %s\n' "$*"; }

[[ "$(uname -s)" == Linux && "$EUID" == 0 ]] || fail 'requires disposable Linux as root'
[[ "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]] || fail 'set PARTNERCONSOLE_DISPOSABLE_HOST=1 on a disposable host only'
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || fail 'requires Ubuntu 24.04'
release_origin="${RELEASE_ORIGIN:?set RELEASE_ORIGIN to a repository with the release tag}"
browser_user="${BROWSER_USER:?set BROWSER_USER to a non-root account for the browser}"
[[ "$(id -u "$browser_user")" != 0 ]] || fail 'the browser account must not be root'
browser="${CHROME:-$(command -v google-chrome || command -v chromium || true)}"
[[ -n "$browser" && -x "$browser" ]] || fail 'Chrome or Chromium is required'

rc6_tag=v1.0.3-rc.6
baseline_tag="${INSTALL_BASELINE:-v1.0.3}"
case "$baseline_tag" in
  v1.0.3-rc.6|v1.0.3-rc.7|v1.0.3|v1.0.4) ;;
  *) fail 'unsupported installer baseline' ;;
esac
origin=https://console.example.test
if [[ "$baseline_tag" != "$rc6_tag" ]]; then
  origin=https://localhost:18443
  : "${PLAYWRIGHT_MODULE:?set PLAYWRIGHT_MODULE to the pinned browser driver}"
fi
export PARTNERCONSOLE_ROOT=/srv/pc-cycle
[[ ! -e "$PARTNERCONSOLE_ROOT" ]] || fail "$PARTNERCONSOLE_ROOT already exists"
app="$PARTNERCONSOLE_ROOT"
work="$(mktemp -d)"
chmod 755 "$work"
PC_FIXTURE_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
export PC_FIXTURE_ADMIN_PASSWORD

checkout_git() { git -c safe.directory="$repo" -C "$repo" "$@"; }
new_tag="$(sed -n 's/^RELEASE_TAG="\(.*\)"$/\1/p' "$repo/partnerconsole.sh")"
new_commit="$(git -c safe.directory="$release_origin" -C "$release_origin" rev-parse "refs/tags/${new_tag}^{commit}")"
source_commit="$(checkout_git rev-parse HEAD)"
origin_git() { git -c safe.directory="$release_origin" -C "$release_origin" "$@"; }
origin_git show "$new_commit:partnerconsole.sh" > "$work/target.sh"
if [[ "$new_commit" != "$source_commit" ]]; then
  [[ "$(origin_git rev-parse "$new_commit^")" == "$source_commit" &&
     "$(origin_git diff --name-only "$source_commit" "$new_commit")" == partnerconsole.sh ]] ||
    fail "private candidate differs from the checkout"
  cmp -s <(sed '/^RELEASE_TREE_SHA256=/d' "$repo/partnerconsole.sh") \\
    <(sed '/^RELEASE_TREE_SHA256=/d' "$work/target.sh") || fail "private installer differs beyond its tree pin"
fi
printf 'testing %s at %s (checkout %s)\n' "$new_tag" "$new_commit" "$source_commit"

baseline_tree="$work/baseline"
if [[ "$baseline_tag" == v1.0.4 ]]; then
  git -c safe.directory="$repo" clone --quiet --no-local --branch "$baseline_tag" --single-branch "$repo" "$baseline_tree"
  grep -Fxq 'RELEASE_TAG="v1.0.4"' "$baseline_tree/partnerconsole.sh" || fail 'v1.0.4 baseline installer'
  baseline_pin="$(sed -n 's/^RELEASE_TREE_SHA256="\([^"]*\)"$/\1/p' "$baseline_tree/partnerconsole.sh")"
else
  fixtures="$repo/dev/tests/fixtures/upgrade"
  (cd "$fixtures" && sha256sum --check --strict SHA256SUMS) || fail 'fixture archive checksum'
  mkdir "$baseline_tree"
  tar -xzf "$fixtures/$baseline_tag.tar.gz" -C "$baseline_tree"
  git -C "$baseline_tree" init -q
  git -C "$baseline_tree" add --all
  git -C "$baseline_tree" -c user.name=Fixture -c user.email=fixture@example.invalid commit -qm "Install fixture $baseline_tag"
  git -C "$baseline_tree" tag "$baseline_tag"
  baseline_pin="$(
    export PARTNERCONSOLE_LIBRARY=1
    # shellcheck disable=SC1091
    source "$repo/partnerconsole.sh"
    tree_sha256 "$baseline_tree"
  )"
fi
baseline_commit="$(git -C "$baseline_tree" rev-parse HEAD)"
cp "$baseline_tree/partnerconsole.sh" "$work/baseline.sh"

# The tests answer the installer prompts and replace only install_deps, whose
# package, firewall and SSH guard work changes the host outside this check.
cat > "$work/overrides.sh" <<EOF
install_deps() { node_ok || install_node; node_ok && python_ok && mysql_version_ok; }
ask_origin() { printf '%s\n%s\n' '$origin' '$origin'; }
ask_admin_password() { printf '%s\n' "\$PC_FIXTURE_ADMIN_PASSWORD"; }
OFFICIAL_REPO="\${PC_RELEASE_ORIGIN:-\$OFFICIAL_REPO}"
RELEASE_TREE_SHA256="\${PC_RELEASE_TREE_SHA256:-\$RELEASE_TREE_SHA256}"
EOF

# Runs an installer function from the given partnerconsole.sh in a new Bash, so
# errexit stays active inside the installer even when the caller tests the status.
run_with() {
  local script="$1"
  shift
  # shellcheck disable=SC2016
  bash -c 'set -euo pipefail; PARTNERCONSOLE_LIBRARY=1; source "$1"; source "$2"; resolve_root; validate_root; shift 2; "$@"' \
    run_with "$script" "$work/overrides.sh" "$@"
}

cleanup() {
  local result=$?
  if [[ "$result" != 0 ]]; then
    for log_file in "$app/logs/api.log" "$app/logs/web.log" "$work/panel.log"; do
      if [[ -f "$log_file" ]]; then tail -n 20 "$log_file" >&2; fi
    done
  fi
  if [[ -n "${probe_pid:-}" ]]; then kill "$probe_pid" 2>/dev/null || true; wait "$probe_pid" 2>/dev/null || true; fi
  if [[ -n "${lab_pid:-}" ]]; then kill "$lab_pid" 2>/dev/null || true; wait "$lab_pid" 2>/dev/null || true; fi
  if [[ -f "$app/stop.sh" ]]; then runuser -u ppv1 -- bash "$app/stop.sh" >/dev/null 2>&1 || true; fi
  rm -rf -- "$work"
  exit "$result"
}
trap cleanup EXIT

root_sql() { mysql --protocol=socket -N -e "$1"; }
service_field() { sed -n "s/^$2=//p" "$app/runtime/pids/$1.id"; }
wait_for_browser() {
  local attempt
  for ((attempt=0; attempt<180; attempt++)); do
    [[ -f "$work/browser/$1" ]] && return
    kill -0 "$probe_pid" 2>/dev/null || break
    sleep 1
  done
  cat "$work/browser.log" >&2
  fail "browser did not finish $1"
}
release_field() { sed -n "s/^$1=//p" "$app/.release/current"; }

browser_home="$(getent passwd "$browser_user" | cut -d: -f6)"
login_url() {
  local result
  result="$(runuser -u "$browser_user" -- env PATH="$PATH" HOME="$browser_home" \
    node "$repo/dev/tests/installer/login-origin-probe.cjs" "$browser" "http://127.0.0.1:3741$1")" ||
    fail "the browser probe failed for $1"
  node -e '
    const r = JSON.parse(process.argv[1]);
    const hits = [...r.login, ...r.blocked.filter((b) => b.url.includes("/auth/login"))];
    if (!hits.length || hits.some((h) => h.method !== "POST")) process.exit(1);
    console.log([...new Set(hits.map((h) => h.url))].join(" "));
  ' "$result" || fail "no POST sign-in request was captured on $1: $result"
}
expect_login_origin() {
  local want="$1" label="$2" admin partner
  admin="$(login_url /gate/k3p8n2)"
  partner="$(login_url /)"
  [[ "$admin" == "$want/api/admin/auth/login" ]] || fail "$label: administrator sign-in used $admin, expected $want/api/admin/auth/login"
  [[ "$partner" == "$want/api/partner/auth/login" ]] || fail "$label: partner sign-in used $partner, expected $want/api/partner/auth/login"
  ok "$label: installed browser sign-in requests use $want"
}
installed_bundle_ok() {
  (
    PP_ROOT="$app"
    PP_DOMAIN_LIBRARY=1
    # shellcheck disable=SC1091
    source "$repo/scripts/domain.sh"
    pp_domain_bundle_ok "$origin"
  )
}

# MySQL 8.4 from the pinned MySQL APT configuration package, local only.
mysql_setup() {
  if ! mysql_version_ok; then
    write_mysql_local_config
    install_mysql_packages
  fi
  assert_mysql_config_access
  systemctl restart mysql
  wait_mysql
  mysql_runtime_is_safe
  mysql_version_ok
}
export -f mysql_setup
run_with "$repo/partnerconsole.sh" mysql_setup || fail 'MySQL 8.4 setup'
ok "MySQL $(mysqld --version | awk '{print $3}') listens on loopback only"

cat > "$work/answer.py" <<'PY'
import os, pty, re, select, signal, sys, time
args = sys.argv[1:]
split = args.index("--")
pairs = list(zip(args[:split:2], args[1:split:2]))
command = args[split + 1:]
pid, fd = pty.fork()
if pid == 0:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    os.execvp(command[0], command)
seen = b""
deadline = time.time() + 900
while time.time() < deadline:
    ready, _, _ = select.select([fd], [], [], 1)
    if not ready:
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    seen += data
    if pairs and re.search(re.escape(pairs[0][0]).encode(), seen):
        os.write(fd, pairs.pop(0)[1].encode() + b"\n")
        seen = b""
else:
    os.kill(pid, 9)
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
PY
db_state() {
  local table
  root_sql "SELECT table_name FROM information_schema.tables WHERE table_schema='partnerconsole' ORDER BY table_name" > "$work/tables"
  printf 'tables %s\n' "$(tr '\n' ' ' < "$work/tables")"
  while IFS= read -r table; do
    root_sql "CHECKSUM TABLE \`partnerconsole\`.\`$table\` EXTENDED"
  done < "$work/tables"
  root_sql "SELECT version_num FROM partnerconsole.alembic_version"
  root_sql "SELECT username, balance_toman, banned FROM partnerconsole.partners ORDER BY username"
}
PC_RELEASE_ORIGIN="$baseline_tree" PC_RELEASE_TREE_SHA256="$baseline_pin" \
  run_with "$work/baseline.sh" setup_app || fail "install $baseline_tag"
[[ "$(release_field tag)" == "$baseline_tag" && "$(release_field commit)" == "$baseline_commit" ]] || fail 'baseline release marker'
ok "official $baseline_tag install from the checked baseline fixture"
if [[ "$baseline_tag" == "$rc6_tag" ]]; then
  # RC6 can record a service's process group before setsid has run and then
  # report a running service as exited. Retry RC6 only; later releases must not.
  rc6_service() {
    local attempt
    run_with "$work/baseline.sh" "$1" && return 0
    for attempt in 1 2; do
      printf 'RC6 %s failed; retry %s after stopping untracked ppv1 processes\n' "$1" "$attempt"
      pkill -KILL -u ppv1 || true
      sleep 2
      rm -f -- "$app/runtime/pids/"*.id "$app/runtime/pids/"*.pid
      run_with "$work/baseline.sh" start_app && return 0
    done
    return 1
  }
  rc6_service start_app || fail 'RC6 start'
  expect_login_origin "$origin" 'RC6 install.sh build'

  # RC6 Domain & SSL rebuilt the web app without loading config/web.env.
  (
    unset NEXT_PUBLIC_API_BASE
    PP_ROOT="$app"
    PP_DOMAIN_LIBRARY=1
    # shellcheck disable=SC1091
    source "$app/scripts/domain.sh"
    pp_domain_build_web > "$work/rc6-domain-build.log" 2>&1
  ) || fail "RC6 Domain & SSL build: $(tail -n 5 "$work/rc6-domain-build.log")"
  run_with "$work/baseline.sh" apply_permissions
  rc6_service restart_app || fail 'RC6 restart after the Domain & SSL build'
  expect_login_origin http://127.0.0.1:8741 'RC6 Domain & SSL rebuild'
  if installed_bundle_ok; then fail 'the RC7 bundle check accepted the RC6 Domain & SSL build'; fi
  ok 'the RC7 bundle check rejects the installed RC6 Domain & SSL build'
else
  fixture_admin="admin"
  fixture_password="$PC_FIXTURE_ADMIN_PASSWORD"
  if [[ "$baseline_tag" == v1.0.3 ]]; then
    fixture_admin=fixture_owner
    fixture_password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    python3 "$work/answer.py" 'Choice: ' 1 'Admin id: ' 1 'New username: ' "$fixture_admin" \
      'Choice: ' 2 'Admin id: ' 1 'New password: ' "$fixture_password" 'Repeat password: ' "$fixture_password" \
      'Choice: ' 0 -- /usr/local/bin/partnerconsole credentials >"$work/credentials.log" 2>&1
    grep -Fq 'Password updated.' "$work/credentials.log" || fail 'Stable Credentials password change'
    grep -Fxq "ADMIN_USERNAME=$fixture_admin" "$app/config/api.env" || fail 'Stable Credentials username change'
  fi
  sed -i 's|^PANEL_PRIVATE_ORIGINS=$|PANEL_PRIVATE_ORIGINS=http://127.0.0.1:18788|' "$app/config/api.env"
  run_with "$work/baseline.sh" start_app || fail "start $baseline_tag"
  expect_login_origin "$origin" "$baseline_tag install"
  # The service account can read the fixture without access to the runner's home.
  install -d -m 755 "$work/panel/v2lab"
  install -m 644 "$repo/dev/v2lab/"{__init__,server}.py "$work/panel/v2lab/"
  install -m 644 "$repo/dev/tests/backend/financial_panel_lab.py" "$work/panel/"
  runuser -u ppv1 -- env PYTHONPATH="$work/panel" "$app/backend/.venv/bin/python" -m uvicorn \
    financial_panel_lab:app --host 127.0.0.1 --port 18788 --no-access-log >"$work/panel.log" 2>&1 &
  lab_pid=$!
  install -d -o "$browser_user" -m 700 "$work/browser"
  runuser -u "$browser_user" -- openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
    -addext subjectAltName=DNS:localhost -keyout "$work/browser/key.pem" -out "$work/browser/cert.pem" >"$work/tls.log" 2>&1
  runuser -u "$browser_user" -- env PATH="$PATH" HOME="$browser_home" PLAYWRIGHT_MODULE="$PLAYWRIGHT_MODULE" \
    PC_FIXTURE_ADMIN="$fixture_admin" PC_FIXTURE_PASSWORD="$fixture_password" \
    node "$repo/dev/tests/installer/financial-upgrade-browser.cjs" "$browser" "$work/browser" "$origin" >"$work/browser.log" 2>&1 &
  probe_pid=$!
  wait_for_browser ready
fi

db_state > "$work/state-before-update"
for file in api web; do
  cp "$app/config/$file.env" "$work/$file.env"
done
declare -A before_pid before_start
for service in api web; do
  before_pid[$service]="$(service_field "$service" pid)"
  before_start[$service]="$(service_field "$service" starttime)"
done
PC_RELEASE_ORIGIN="$release_origin" \
  run_with "$work/target.sh" update_app || fail "official Update to $new_tag"
[[ "$(release_field tag)" == "$new_tag" && "$(release_field commit)" == "$new_commit" ]] || fail "$new_tag release marker"
for service in api web; do
  [[ "$(service_field "$service" pid)" != "${before_pid[$service]}" &&
     "$(service_field "$service" starttime)" != "${before_start[$service]}" &&
     "$(service_field "$service" uid)" == "$(id -u ppv1)" ]] || fail "Update did not restart $service as ppv1"
done
cmp -s "$app/config/api.env" "$work/api.env" && cmp -s "$app/config/web.env" "$work/web.env" ||
  fail 'Update changed the installed configuration or secrets'
db_state | cmp -s - "$work/state-before-update" || fail 'Update changed schema or fixture data'
curl -fsS http://127.0.0.1:8741/api/health >/dev/null || fail 'API health after Update'
curl -fsS -o /dev/null -D "$work/cors" -X OPTIONS http://127.0.0.1:8741/api/partner/subscriptions \
  -H "Origin: $origin" -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: authorization,content-type'
grep -Fixq "access-control-allow-origin: $origin" <(tr -d '\r' <"$work/cors") || fail 'configured HTTPS CORS origin after Update'
run_with "$repo/partnerconsole.sh" app_running || fail 'Update left the application stopped'
ok "official Update from $baseline_tag to $new_tag verified the tree pin and restarted the running app"
expect_login_origin "$origin" "Update to $new_tag"
installed_bundle_ok || fail "the bundle check rejected the build from the Update to $new_tag"
ok "the bundle check accepts the web app rebuilt by the Update"

if [[ -n "${probe_pid:-}" ]]; then
  touch "$work/browser/updated"
  wait_for_browser complete
  wait "$probe_pid" || { cat "$work/browser.log" >&2; fail 'browser upgrade checks'; }
  cat "$work/browser.log"
fi

# Full Restore with fixture data.
restore() {
  local choice="$1" output="$2"
  sleep 1
  python3 "$work/answer.py" \
    'Backup number from the top, starting at 1: ' "$choice" \
    'Type restore partnerconsole to replace the database: ' 'restore partnerconsole' \
    -- bash "$app/scripts/backup.sh" restore > "$output" 2>&1
}
list_index() {
  bash "$app/scripts/backup.sh" list | awk -v name="$1" 'NR > 1 {i++; if ($1 == name) print i}'
}
trial_left() {
  [[ -n "$(root_sql "SHOW DATABASES LIKE 'partnerconsole_restore_check'")$(root_sql "SELECT user FROM mysql.user WHERE user='pc_restore_check'")" ]]
}

run_with "$repo/partnerconsole.sh" stop_app || fail 'official Stop'
root_sql "INSERT INTO partnerconsole.partners (username, password_hash, balance_toman, banned, created_at, updated_at) VALUES
  ('fixture-a', 'fixture-not-a-hash', 1500, 0, NOW(6), NOW(6)),
  ('fixture-b', 'fixture-not-a-hash', 2500, 1, NOW(6), NOW(6))"
db_state > "$work/state-backup"
grep -Fxq '0005_security_state' "$work/state-backup" || fail 'unexpected migration version before backup'
run_with "$repo/partnerconsole.sh" backup_app > "$work/backup.log" 2>&1 || fail "official Backup: $(tail -n 5 "$work/backup.log")"
mapfile -t dumps < <(find "$app/backups" -maxdepth 1 -name 'partnerconsole-*.sql' -printf '%f\n' | sort)
[[ "${#dumps[@]}" == 1 ]] || fail 'Backup did not write exactly one dump'
backup_name="${dumps[0]}"
[[ "$(stat -c '%u:%a' "$app/backups/$backup_name" "$app/backups/$backup_name.sha256" | sort -u)" == 0:600 ]] || fail 'backup files are not root:0600'
bash "$app/scripts/backup.sh" list | grep -Eq "^$backup_name .* database valid$" || fail 'the backup checksum is not valid'
ok 'official Backup wrote a root-only MySQL dump with a valid checksum'

root_sql "UPDATE partnerconsole.partners SET balance_toman = 999999 WHERE username = 'fixture-a';
  DELETE FROM partnerconsole.partners WHERE username = 'fixture-b';
  INSERT INTO partnerconsole.partners (username, password_hash, balance_toman, banned, created_at, updated_at)
  VALUES ('fixture-c', 'fixture-not-a-hash', 42, 0, NOW(6), NOW(6))"
db_state > "$work/state-changed"
! cmp -s "$work/state-backup" "$work/state-changed" || fail 'the data change after Backup was not visible'

record_checksum() {
  (
    PP_BACKUP_LIBRARY=1
    # shellcheck disable=SC1091
    source "$app/scripts/backup.sh"
    pp_backup_record_checksum "$1" "$2"
  )
}
broken="$app/backups/partnerconsole-20000101T000000Z.sql"
{ cat "$app/backups/$backup_name"; printf 'INSERT INTO pc_missing_table VALUES (1);\n'; } > "$broken"
record_checksum "$broken" 20000101T000000Z
restore "$(list_index "$(basename "$broken")")" "$work/restore-broken" && fail 'a dump that fails to import was restored'
grep -Fq 'The trial import failed. The main database was not changed.' "$work/restore-broken" || { cat "$work/restore-broken" >&2; fail 'trial import failure message'; }
db_state | cmp -s - "$work/state-changed" || fail 'a failed trial import changed the main database'
if trial_left; then fail 'the trial database or account was left behind'; fi
ok 'a dump that fails to import stops at the trial import and leaves the main database unchanged'

tampered="$app/backups/partnerconsole-20000102T000000Z.sql"
cp "$app/backups/$backup_name" "$tampered"
record_checksum "$tampered" 20000102T000000Z
printf -- '-- changed after the checksum\n' >> "$tampered"
restore "$(list_index "$(basename "$tampered")")" "$work/restore-tampered" && fail 'a changed dump was restored'
grep -Fq 'The checksum does not match. The file was not restored.' "$work/restore-tampered" || { cat "$work/restore-tampered" >&2; fail 'checksum mismatch message'; }
db_state | cmp -s - "$work/state-changed" || fail 'a checksum mismatch changed the main database'
ok 'a dump changed after its checksum is refused before any import'

protective_before="$(find "$app/backups" -maxdepth 1 -name 'partnerconsole-20[1-9]*.sql' | wc -l)"
restore "$(list_index "$backup_name")" "$work/restore-good" || { cat "$work/restore-good" >&2; fail 'official Full Restore'; }
grep -Fq 'Restore finished.' "$work/restore-good" || fail 'Restore did not report success'
db_state | cmp -s - "$work/state-backup" || { diff <(db_state) "$work/state-backup" >&2 || true; fail 'restored data differs from the backup'; }
if trial_left; then fail 'the trial database or account was left behind after Restore'; fi
newest="$(find "$app/backups" -maxdepth 1 -name 'partnerconsole-20[1-9]*.sql' -printf '%f\n' | sort | tail -n 1)"
[[ "$(find "$app/backups" -maxdepth 1 -name 'partnerconsole-20[1-9]*.sql' | wc -l)" -gt "$protective_before" ]] || fail 'Restore took no protective backup'
grep -Fq "'fixture-c'" "$app/backups/$newest" || fail 'the protective backup does not hold the data from before Restore'
ok 'official Full Restore returned every table to the backup, kept migration 0005_security_state and retained a protective backup'

run_with "$repo/partnerconsole.sh" start_app || fail 'start after Restore'
[[ "$(curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1:8741/api/health)" == 200 ]] || fail 'API health after Restore'
run_with "$repo/partnerconsole.sh" stop_app
ok 'the application starts on the restored database'
printf 'install, update and restore checks passed\n'
