#!/usr/bin/env bash
# Local checks for domain, backup safety, and credential helpers. No server.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
export PP_DOMAIN_LIBRARY=1
export PP_BACKUP_LIBRARY=1
export PP_ROOT="$repo"
# shellcheck disable=SC1091
source "$repo/scripts/domain.sh"
# shellcheck disable=SC1091
source "$repo/scripts/backup.sh"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok %s\n' "$*"; }

pp_domain_valid "console.example.com" || fail "a normal domain was rejected"
pp_domain_valid "example.com" || fail "a lowercase domain was rejected"
if pp_domain_valid "bad_domain"; then fail "an invalid domain was accepted"; fi
if pp_domain_valid "127.0.0.1"; then fail "an address was accepted as a domain"; fi
if pp_domain_valid "*.example.com"; then fail "a wildcard domain was accepted"; fi
ok "domain names"

pp_domain_dns_ok "203.0.113.10" "" "203.0.113.10" "" || fail "a matching address was rejected"
if pp_domain_dns_ok "203.0.113.10" "" "203.0.113.11" ""; then fail "a different address was accepted"; fi
if pp_domain_dns_ok "203.0.113.10" "" $'203.0.113.10\n203.0.113.11' ""; then
  fail "an extra address was accepted"
fi
pp_ufw_allows_web $'Status: active\n80/tcp ALLOW\n443/tcp ALLOW' || fail "both web ports were not recognized"
if pp_ufw_allows_web $'Status: active\n80/tcp ALLOW'; then fail "a firewall missing 443 was accepted"; fi
if pp_domain_dns_ok "203.0.113.10" "" "203.0.113.10" "2001:db8::1"; then
  fail "an unmatched IPv6 record was accepted"
fi
pp_domain_dns_ok "203.0.113.10" "2001:db8::1" "203.0.113.10" "2001:db8::1" || fail "matching IPv6 was rejected"
if pp_domain_dns_ok "203.0.113.10" "" "203.0.113.10" "::ffff:203.0.113.10"; then
  fail "an IPv4-mapped address was accepted"
fi
pp_domain_dns_ok "65.109.208.176" "" "65.109.208.176" "" || fail "an A record without AAAA was rejected"
ok "dns match"

if pp_foreign_web_listener $'LISTEN 0 0 0.0.0.0:80 users:(("apache",pid=1))'; then
  true
else
  fail "a foreign web listener was allowed"
fi
if pp_foreign_web_listener $'LISTEN 0 0 0.0.0.0:80 users:(("nginx",pid=1))'; then
  fail "nginx on port 80 was treated as a conflict"
fi
ok "port conflict"

work="$(mktemp -d)"
PP_NGINX_SITE="$work/sites-available/partnerconsole.conf"
PP_NGINX_LINK="$work/sites-enabled/partnerconsole.conf"
PP_SYSTEMD_UNIT="$work/partnerconsole.service"
PP_ACME_ROOT="$work/acme"
mkdir -p "$work/sites-available" "$work/sites-enabled"
printf 'other-site\n' > "$work/sites-available/other.conf"
pp_nginx_https_config "console.example.com" > "$PP_NGINX_SITE"
ln -s "$PP_NGINX_SITE" "$PP_NGINX_LINK"
grep -q 'proxy_pass http://127.0.0.1:8741;' "$PP_NGINX_SITE" || fail "the API proxy is missing"
grep -q 'proxy_pass http://127.0.0.1:3741;' "$PP_NGINX_SITE" || fail "the web proxy is missing"
grep -q 'server_name console.example.com;' "$PP_NGINX_SITE" || fail "the server name is missing"
grep -q 'X-Forwarded-Proto https;' "$PP_NGINX_SITE" || fail "the forwarded protocol is missing"
printf '%s\n%s\n' "$PP_NGINX_SITE" "$PP_NGINX_LINK" > "$work/state"
pp_domain_rollback "$work/state"
[[ ! -e "$PP_NGINX_SITE" && ! -e "$PP_NGINX_LINK" ]] || fail "rollback left the Partner Console site"
grep -q 'other-site' "$work/sites-available/other.conf" || fail "rollback removed another site"
unit="$(pp_systemd_unit_text)"
grep -q 'ExecStart=/usr/local/bin/partnerconsole start' <<<"$unit" || fail "boot service does not use the existing start command"
grep -q 'User=root' <<<"$unit" || fail "boot service user was changed"
ok "nginx config and rollback"

mkdir -p "$work/backups" "$work/config" "$work/runtime/pids" "$work/logs" "$work/data" "$work/.release"
PP_ROOT="$work"
printf 'CREATE TABLE admin_users(id int);\nINSERT INTO admin_users VALUES (1);\n' > "$work/backups/partnerconsole-old.sql"
chmod 600 "$work/backups/partnerconsole-old.sql"
pp_backup_record_checksum "$work/backups/partnerconsole-old.sql" "old"
pp_backup_checksum_state "$work/backups/partnerconsole-old.sql" || fail "a matching checksum was rejected"
pp_backup_accept "$work/backups/partnerconsole-old.sql" || fail "a checksummed backup was rejected"
printf 'changed\n' >> "$work/backups/partnerconsole-old.sql"
if pp_backup_checksum_state "$work/backups/partnerconsole-old.sql"; then
  fail "a damaged backup was accepted"
fi
printf 'CREATE TABLE ok(id int);\n' > "$work/backups/partnerconsole-plain.sql"
if pp_backup_sql_rejected "$work/backups/partnerconsole-plain.sql"; then
  fail "a normal dump was rejected"
fi
printf 'GRANT ALL ON *.* TO root;\n' > "$work/backups/partnerconsole-evil.sql"
pp_backup_sql_rejected "$work/backups/partnerconsole-evil.sql" || fail "GRANT was accepted"
ln -s /etc/passwd "$work/backups/partnerconsole-link.sql" || true
if [[ -L "$work/backups/partnerconsole-link.sql" ]]; then
  if pp_backup_inside "$work/backups/partnerconsole-link.sql"; then
    fail "a symlink backup was accepted"
  fi
else
  echo "SYMLINK_BACKUP_NOT_TESTED"
fi
printf 'CREATE TABLE old_only(id int);\n' > "$work/backups/partnerconsole-v102.sql"
chmod 600 "$work/backups/partnerconsole-v102.sql"
if pp_backup_accept "$work/backups/partnerconsole-v102.sql"; then
  fail "a backup without a checksum was accepted"
fi
pp_backup_known_version "0005_security_state" || fail "the current migration was rejected"
if pp_backup_known_version "9999_future"; then fail "an unknown migration was accepted"; fi
ok "backup safety"

grep -q -- '--skip-add-locks' "$repo/scripts/backup.sh" || fail "new backups still emit LOCK TABLES"
printf 'LOCK TABLES `admin_users` WRITE;\n' > "$work/backups/partnerconsole-locked.sql"
pp_backup_dump_locks "$work/backups/partnerconsole-locked.sql" || fail "an old dump with LOCK TABLES was not recognized"
printf 'CREATE TABLE admin_users(id int);\n' > "$work/backups/partnerconsole-unlocked.sql"
if pp_backup_dump_locks "$work/backups/partnerconsole-unlocked.sql"; then fail "a dump without LOCK TABLES was treated as locked"; fi
grant_sql="$(pp_backup_main_lock_grant)"
[[ "$grant_sql" == "GRANT LOCK TABLES ON \`partnerconsole\`.* TO 'partnerconsole'@'127.0.0.1'" ]] || fail "the main lock grant is not limited to partnerconsole"
[[ "$grant_sql" != *'*.*'* ]] || fail "the main lock grant is global"
printf '%s\n' "GRANT USAGE ON *.* TO 'pc_restore_check'@'127.0.0.1'" "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, LOCK TABLES ON \`partnerconsole_restore_check\`.* TO 'pc_restore_check'@'127.0.0.1'" | pp_backup_grants_are_limited || fail "a trial grant was rejected"
if printf '%s\n' "GRANT ALL PRIVILEGES ON \`partnerconsole\`.* TO 'pc_restore_check'@'127.0.0.1'" | pp_backup_grants_are_limited; then
  fail "a grant on the main database was accepted"
fi
if printf '%s\n' "GRANT ALL PRIVILEGES ON *.* TO 'pc_restore_check'@'127.0.0.1'" | pp_backup_grants_are_limited; then
  fail "a global trial grant was accepted"
fi

trial_case() {
  local mode="$1" log="$work/trial.log"
  : > "$log"
  rm -f "$work/db-exists" "$work/user-exists" "$work/fail-db" "$work/fail-user" "$work/fail-import" "$work/imported.sql"
  case "$mode" in
    exists) touch "$work/db-exists" ;;
    fail-db) touch "$work/fail-db" ;;
    fail-user) touch "$work/fail-user" ;;
    fail-import) touch "$work/fail-import" ;;
  esac
  pp_backup_root_mysql() {
    printf 'ROOT %s\n' "$*" >> "$log"
    case "$*" in
      *"SHOW DATABASES LIKE 'partnerconsole_restore_check'"*)
        [[ -f "$work/db-exists" || -f "$work/db-created" ]] && printf 'partnerconsole_restore_check\n'
        ;;
      *"SELECT user FROM mysql.user WHERE user='pc_restore_check'"*)
        [[ -f "$work/user-exists" || -f "$work/user-created" ]] && printf 'pc_restore_check\n'
        ;;
      *"CREATE DATABASE partnerconsole_restore_check"*)
        [[ -f "$work/fail-db" ]] && return 1
        touch "$work/db-created"
        ;;
      *"CREATE USER 'pc_restore_check'@'127.0.0.1'"*)
        [[ -f "$work/fail-user" ]] && return 1
        touch "$work/user-created"
        ;;
      *"GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, LOCK TABLES ON partnerconsole_restore_check."*) ;;
      *"SHOW GRANTS FOR 'pc_restore_check'@'127.0.0.1'"*)
        printf '%s\n' "GRANT USAGE ON *.* TO 'pc_restore_check'@'127.0.0.1'" "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, LOCK TABLES ON \`partnerconsole_restore_check\`.* TO 'pc_restore_check'@'127.0.0.1'"
        ;;
      *"table_schema='partnerconsole'"*|*"FROM partnerconsole.admin_users"*)
        printf '11\n'
        ;;
      *"DROP USER 'pc_restore_check'@'127.0.0.1'"*)
        rm -f "$work/user-created"
        ;;
      *"DROP DATABASE partnerconsole_restore_check"*)
        rm -f "$work/db-created"
        ;;
    esac
    return 0
  }
  pp_backup_client_mysql() {
    printf 'CLIENT %s\n' "$*" >> "$log"
    if [[ "$*" == *"FROM partnerconsole.admin_users"* ]]; then
      return 1
    fi
    if [[ "$*" == *"version_num"* ]]; then
      printf '0005_security_state\n'
      return 0
    fi
    if [[ "$*" == *" -e "* ]]; then
      return 0
    fi
    cat > "$work/imported.sql"
    [[ -f "$work/fail-import" ]] && return 1
    return 0
  }
  set +e
  pp_backup_trial_import "$work/backups/partnerconsole-locked.sql"
  local status=$?
  set -e
  printf '%s\n' "$status"
}

rm -f "$work/db-created" "$work/user-created"
status="$(trial_case exists)"
[[ "$status" == "1" ]] || fail "an existing trial database was not refused"
if grep -q 'CREATE DATABASE\|DROP DATABASE\|DROP USER' "$work/trial.log"; then
  fail "an existing trial name was changed"
fi
rm -f "$work/db-created" "$work/user-created"
status="$(trial_case fail-user)"
[[ "$status" == "1" ]] || fail "a failed trial account was treated as success"
grep -q 'DROP DATABASE partnerconsole_restore_check' "$work/trial.log" || fail "the trial database created by this run was left behind"
if grep -q 'DROP USER' "$work/trial.log"; then fail "a trial account that was not created was removed"; fi
rm -f "$work/db-created" "$work/user-created"
status="$(trial_case fail-import)"
[[ "$status" == "1" ]] || fail "a failed import was treated as success"
grep -q 'DROP DATABASE partnerconsole_restore_check' "$work/trial.log" || fail "cleanup skipped the trial database after a failed import"
grep -q 'DROP USER' "$work/trial.log" || fail "cleanup skipped the trial account after a failed import"
if grep -q 'DROP DATABASE partnerconsole$' "$work/trial.log"; then fail "cleanup removed the main database"; fi
rm -f "$work/db-created" "$work/user-created" "$work/imported.sql"
status="$(trial_case ok)"
[[ "$status" == "0" ]] || fail "a locked dump was not imported"
grep -q 'LOCK TABLES' "$work/imported.sql" || fail "the locked dump was not the file that was imported"
grep -q '0005' <<<"$(pp_backup_known_version 0005_security_state && echo 0005)" || fail "the migration check was skipped"
ok "trial import controls"

empty="$(mktemp -d)"
( PATH="$empty"; pp_domain_missing_packages ) | grep -qx nginx || fail "a missing nginx package was not reported"
( PATH="$empty"; pp_domain_missing_packages ) | grep -qx certbot || fail "a missing certbot package was not reported"
pp_domain_confirm() { return 1; }
pp_domain_apt() { echo APT >> "$work/apt.log"; return 0; }
: > "$work/apt.log"
if ( PATH="$empty"; pp_domain_prepare_packages ); then fail "package installation continued without approval"; fi
[[ ! -s "$work/apt.log" ]] || fail "packages were installed after the operator refused"
pp_domain_confirm() { return 0; }
pp_domain_apt() { echo APT >> "$work/apt.log"; return 1; }
if ( PATH="$empty"; pp_domain_prepare_packages ); then fail "a failed package installation was ignored"; fi
[[ ! -e "$PP_NGINX_SITE" ]] || fail "a failed package installation wrote a site"
grep -q 'nginx and certbot are not installed here' "$repo/partnerconsole.sh" || fail "normal install can install the web packages"
ok "domain packages"

saved="$work/sites-available/previous.conf"
printf 'previous-site\n' > "$saved"
mkdir -p "$work/.release" "$work/config" "$work/.next"
printf 'old-build\n' > "$work/.next/marker"
printf 'CORS_ORIGINS=http://127.0.0.1:3741\n' > "$work/config/api.env"
printf 'NEXT_PUBLIC_API_BASE=http://127.0.0.1:8741\n' > "$work/config/web.env"
printf 'other-site\n' > "$work/sites-available/other.conf"
PP_ROOT="$work"
PP_NGINX_SITE="$work/sites-available/partnerconsole.conf"
PP_NGINX_LINK="$work/sites-enabled/partnerconsole.conf"
PP_ACME_ROOT="$work/acme"
PP_CERT_FULLCHAIN="$work/fullchain.pem"
PP_CERT_PRIVKEY="$work/privkey.pem"
PP_RENEW_HOOK="$work/renew-hook"
PP_DOMAIN_STATE="$work/domain-state"
cp "$saved" "$PP_NGINX_SITE"
ln -sfn "$PP_NGINX_SITE" "$PP_NGINX_LINK"
pp_env_value() { printf 'http://127.0.0.1:3741\n'; }
pp_domain_public_urls() { printf 'changed\n' > "$work/config/changed"; }
pp_domain_restore_urls() { printf 'restored\n' > "$work/config/restored"; rm -f "$work/config/changed"; }
pp_domain_nginx_test() { return 0; }
pp_domain_nginx_reload() { return 0; }
systemctl() { [[ "$*" == 'is-active --quiet nginx' ]] && return 3; return 1; }
pp_domain_acme_probe() { printf 'http\n' > "$work/acme-seen"; grep -q 'acme-challenge' "$PP_NGINX_SITE" && ! grep -q 'ssl_certificate ' "$PP_NGINX_SITE"; }
pp_domain_issue_cert() { cp "$PP_NGINX_SITE" "$work/during-cert.conf"; printf 'cert\n' > "$PP_CERT_FULLCHAIN"; printf 'key\n' > "$PP_CERT_PRIVKEY"; return 1; }
pp_domain_build_web() { printf 'new-build\n' > "$work/.next/marker"; return 1; }
pp_domain_bundle_ok() { return 0; }
pp_domain_https_probe() { return 0; }
app_running() { return 1; }
restart_app() { fail "a stopped application was restarted"; }
pp_domain_install_renew_hook() { printf 'hook\n' > "$work/hook-installed"; return 0; }
pp_domain_enable_timer() { printf 'timer\n' > "$work/timer"; }
if pp_domain_activate "console.example.com"; then fail "a failed certificate was treated as active"; fi
grep -q 'acme-challenge' "$work/during-cert.conf" || fail "HTTPS was configured before the certificate"
if grep -q 'ssl_certificate ' "$work/during-cert.conf"; then fail "the temporary site contained SSL before the certificate existed"; fi
grep -q 'previous-site' "$PP_NGINX_SITE" || fail "the previous site was not restored"
grep -q 'other-site' "$work/sites-available/other.conf" || fail "another site was changed"
[[ ! -e "$work/hook-installed" ]] || fail "a renewal hook was installed after a failed certificate"
ok "acme before https and rollback"

printf 'previous-site\n' > "$PP_NGINX_SITE"
printf 'old-build\n' > "$work/.next/marker"
pp_domain_issue_cert() { printf 'cert\n' > "$PP_CERT_FULLCHAIN"; printf 'key\n' > "$PP_CERT_PRIVKEY"; return 0; }
pp_domain_acme_probe() { return 0; }
if pp_domain_activate "console.example.com"; then fail "a failed web build was treated as active"; fi
grep -q 'old-build' "$work/.next/marker" || fail "the previous web build was not restored"
grep -q 'previous-site' "$PP_NGINX_SITE" || fail "the previous site was not restored after the build failed"
grep -q 'restored' "$work/config/restored" || fail "the previous public URLs were not restored"
grep -q 'other-site' "$work/sites-available/other.conf" || fail "another site was changed during build rollback"
ok "build rollback"

pp_domain_build_web() { return 0; }
pp_domain_nginx_test() { if grep -q 'ssl_certificate ' "$PP_NGINX_SITE"; then return 1; fi; return 0; }
if pp_domain_activate "console.example.com"; then fail "a failed HTTPS nginx test was treated as active"; fi
grep -q 'previous-site' "$PP_NGINX_SITE" || fail "a failed HTTPS test left the new site in place"
ok "https nginx test rollback"

pp_domain_nginx_test() { printf 'tested\n' >> "$work/nginx-events"; return 0; }
pp_domain_nginx_reload() { printf 'reloaded\n' >> "$work/nginx-events"; return 1; }
: > "$work/nginx-events"
if pp_domain_activate "console.example.com"; then fail "a failed reload was treated as active"; fi
grep -q 'previous-site' "$PP_NGINX_SITE" || fail "a failed reload left the new site in place"
ok "reload rollback"

bin="$work/bin"
mkdir -p "$bin"
cert_subj() {
  if [[ -n "${MSYSTEM:-}" ]]; then printf '//CN=%s' "$1"; else printf '/CN=%s' "$1"; fi
}
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$PP_CERT_PRIVKEY" -out "$PP_CERT_FULLCHAIN" \
  -subj "$(cert_subj console.example.com)" -addext "subjectAltName=DNS:console.example.com" -days 30 >/dev/null 2>&1
pp_nginx_https_config "console.example.com" > "$PP_NGINX_SITE"
pp_domain_issue_cert() { echo ISSUED >> "$work/issued"; return 0; }
pp_domain_install_renew_hook() { printf 'hook\n' > "$work/repeat-hook"; return 0; }
before="$(cat "$PP_NGINX_SITE")"
if ! pp_domain_activate "console.example.com"; then fail "an existing domain was not left in place"; fi
[[ "$(cat "$PP_NGINX_SITE")" == "$before" ]] || fail "repeating the domain rewrote the active site"
[[ ! -s "$work/issued" ]] || fail "repeating the domain requested another certificate"
grep -q 'hook' "$work/repeat-hook" || fail "repeating the domain skipped the renewal hook"
ok "repeat domain"

hook="$work/renew.sh"
pp_domain_renew_hook_text > "$hook"
chmod +x "$hook"
printf '%s\n' '#!/bin/bash' 'if [[ "$1" == "-t" ]]; then exit "${NGINX_RC:-0}"; fi' 'printf "%s\n" "$*" >> "$HOOK_LOG"' > "$bin/nginx"
printf '%s\n' '#!/bin/bash' 'printf "%s\n" "$*" >> "$HOOK_LOG"' > "$bin/systemctl"
chmod +x "$bin/nginx" "$bin/systemctl"
: > "$work/hook.log"
if ! ( PATH="$bin:$PATH" HOOK_LOG="$work/hook.log" RENEWED_LINEAGE=/etc/letsencrypt/live/other bash "$hook" ); then
  fail "a renewal for another certificate was not ignored"
fi
[[ ! -s "$work/hook.log" ]] || fail "a renewal for another certificate changed nginx"
: > "$work/hook.log"
( PATH="$bin:$PATH" HOOK_LOG="$work/hook.log" RENEWED_LINEAGE=/etc/letsencrypt/live/partnerconsole bash "$hook" ) || fail "the renewal hook rejected a good nginx test"
grep -q 'reload nginx' "$work/hook.log" || fail "the renewal hook did not reload nginx"
: > "$work/hook.log"
if ( PATH="$bin:$PATH" HOOK_LOG="$work/hook.log" NGINX_RC=1 RENEWED_LINEAGE=/etc/letsencrypt/live/partnerconsole bash "$hook" ); then
  fail "the renewal hook reloaded nginx after nginx -t failed"
fi
if grep -q 'reload nginx' "$work/hook.log"; then fail "a failed nginx test still reloaded nginx"; fi
if [[ "$(id -u)" == "0" ]]; then
  pp_domain_install_renew_hook || fail "the renewal hook was not installed"
else
  echo "HOOK_OWNERSHIP_NOT_TESTED"
fi
ok "renewal hook"
# shellcheck disable=SC1091
source "$repo/scripts/domain.sh"
PP_ROOT="$work"
PP_CERT_FULLCHAIN="$work/fullchain.pem"
PP_CERT_PRIVKEY="$work/privkey.pem"

dig_text() {
  case "$1:$PP_DNS_CASE" in
    A:good|A:mapped|A:real6|A:bad6|A:ernemo)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      printf '%s\n' 'test.ernemo.ir. 300 IN A 65.109.208.176'
      ;;
    A:foreign)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      printf '%s\n' 'test.ernemo.ir. 300 IN A 65.109.208.176'
      printf '%s\n' 'test.ernemo.ir. 300 IN A 198.51.100.8'
      ;;
    A:cname)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      printf '%s\n' 'test.ernemo.ir. 300 IN CNAME target.example.net.'
      printf '%s\n' 'target.example.net. 300 IN A 65.109.208.176'
      ;;
    AAAA:good|AAAA:ernemo|AAAA:foreign|AAAA:cname)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      ;;
    AAAA:mapped)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      printf '%s\n' 'test.ernemo.ir. 300 IN AAAA ::ffff:65.109.208.176'
      ;;
    AAAA:real6)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      printf '%s\n' 'test.ernemo.ir. 300 IN AAAA 2001:db8::10'
      ;;
    AAAA:bad6)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1'
      printf '%s\n' 'test.ernemo.ir. 300 IN AAAA 2001:db8::99'
      ;;
    *:nx)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: NXDOMAIN, id: 1'
      ;;
    *:fail)
      printf '%s\n' ';; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 1'
      ;;
  esac
}
pp_domain_dig() {
  case "$PP_DNS_CASE" in
    timeout) return 9 ;;
    missing) return 5 ;;
    *) dig_text "$1"; return 0 ;;
  esac
}
dns_status() {
  local status=0
  PP_DNS_CASE="$1"
  set +e
  pp_domain_lookup test.ernemo.ir "$2" "$3" >/dev/null
  status=$?
  set -e
  printf '%s\n' "$status"
}
[[ "$(dns_status good 65.109.208.176 "")" == "0" ]] || fail "a correct A record and empty AAAA was rejected"
[[ "$(dns_status ernemo 65.109.208.176 "")" == "0" ]] || fail "the simulated test domain was rejected"
[[ "$(dns_status mapped 65.109.208.176 "")" == "1" ]] || fail "an IPv4-mapped answer was treated as empty AAAA"
[[ "$(dns_status real6 65.109.208.176 2001:db8::10)" == "0" ]] || fail "a matching real AAAA was rejected"
[[ "$(dns_status bad6 65.109.208.176 2001:db8::10)" != "0" ]] || fail "a foreign AAAA was accepted"
[[ "$(dns_status foreign 65.109.208.176 "")" != "0" ]] || fail "a foreign A record was accepted"
[[ "$(dns_status cname 65.109.208.176 "")" == "0" ]] || fail "a CNAME to this server was rejected"
[[ "$(dns_status nx 65.109.208.176 "")" == "2" ]] || fail "NXDOMAIN was not reported"
[[ "$(dns_status fail 65.109.208.176 "")" == "3" ]] || fail "SERVFAIL was treated as an empty answer"
[[ "$(dns_status timeout 65.109.208.176 "")" == "4" ]] || fail "a DNS timeout was treated as an empty answer"
[[ "$(dns_status missing 65.109.208.176 "")" == "5" ]] || fail "a missing DNS tool was treated as an empty answer"
ok "dns lookup"

cert_dir="$work/certs"
mkdir -p "$cert_dir"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$cert_dir/good.key" -out "$cert_dir/good.crt" \
  -subj "$(cert_subj test.ernemo.ir)" -addext "subjectAltName=DNS:test.ernemo.ir" -days 30 >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$cert_dir/evil.key" -out "$cert_dir/evil.crt" \
  -subj "$(cert_subj eviltest.ernemo.ir)" -addext "subjectAltName=DNS:eviltest.ernemo.ir" -days 30 >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes -keyout "$cert_dir/old.key" -out "$cert_dir/old.csr" \
  -subj "$(cert_subj test.ernemo.ir)" >/dev/null 2>&1
printf 'subjectAltName=DNS:test.ernemo.ir\n' > "$cert_dir/san.cnf"
# openssl ca sets fixed dates on OpenSSL 3.0, which lacks x509 -not_before.
mkdir -p "$cert_dir/ca"
: > "$cert_dir/ca/index.txt"
printf '01\n' > "$cert_dir/ca/serial"
printf '%s\n' '[ca]' 'default_ca = expired' '[expired]' 'database = ca/index.txt' 'serial = ca/serial' \
  'new_certs_dir = ca' 'default_md = sha256' 'policy = names' '[names]' 'commonName = supplied' > "$cert_dir/ca.cnf"
(cd "$cert_dir" && openssl ca -batch -selfsign -config ca.cnf -keyfile old.key -in old.csr -out old.crt \
  -startdate 20200101000000Z -enddate 20200102000000Z -extfile san.cnf) >/dev/null 2>&1
PP_CERT_FULLCHAIN="$cert_dir/good.crt"
PP_CERT_PRIVKEY="$cert_dir/good.key"
pp_domain_cert_covers test.ernemo.ir || fail "a certificate for the right hostname was rejected"
PP_CERT_FULLCHAIN="$cert_dir/evil.crt"
PP_CERT_PRIVKEY="$cert_dir/evil.key"
if pp_domain_cert_covers test.ernemo.ir; then fail "a lookalike hostname was accepted"; fi
PP_CERT_FULLCHAIN="$cert_dir/old.crt"
PP_CERT_PRIVKEY="$cert_dir/old.key"
if pp_domain_cert_covers test.ernemo.ir; then fail "an expired certificate was accepted"; fi
ok "certificate name and dates"

pp_domain_json_ok '{"ok": true}' || fail "a healthy API body was rejected"
if pp_domain_json_ok '{"ok": false}'; then fail "ok false was accepted"; fi
if pp_domain_json_ok '<html>ok true</html>'; then fail "an HTML body was accepted as API health"; fi
pp_domain_login_body_ok '<h1>ورود</h1>' || fail "the login page was rejected"
if pp_domain_login_body_ok '<h1>ورود</h1>Internal Server Error'; then fail "an error page was accepted"; fi
pp_domain_redirect_ok "https://test.ernemo.ir/login" test.ernemo.ir || fail "a same-host HTTPS login redirect was rejected"
if pp_domain_redirect_ok "http://test.ernemo.ir/login" test.ernemo.ir; then fail "an HTTP redirect was accepted"; fi
if pp_domain_redirect_ok "https://evil.example/login" test.ernemo.ir; then fail "a redirect to another domain was accepted"; fi
pp_domain_fetch() {
  local n=0
  if [[ -f "$work/fetch-n" ]]; then n="$(cat "$work/fetch-n")"; fi
  printf '%s\n' "$((n + 1))" > "$work/fetch-n"
  case "$PP_HTTP_CASE:$2" in
    slow:/api/health)
      [[ "$n" -ge 1 ]] || return 1
      printf '%s\n' 200 - '{"ok": true}'
      ;;
    ok:/api/health|page500:/api/health|api500:/api/health|web502:/api/health|badjson:/api/health|redir:/api/health|badredir:/api/health|asset400:/api/health|cors:/api/health|loopback:/api/health|foreign:/api/health)
      case "$PP_HTTP_CASE" in
        api500) printf '%s\n' 500 - 'no' ;;
        badjson) printf '%s\n' 200 - '{"ok": false}' ;;
        *) printf '%s\n' 200 - '{"ok": true}' ;;
      esac
      ;;
    ok:/|slow:/|asset400:/|cors:/|loopback:/|foreign:/)
      printf '%s\n' 200 - '<h1>ورود</h1><script src="/_next/static/site.js"></script>'
      ;;
    page500:/)
      printf '%s\n' 200 - '<h1>ورود</h1>'
      ;;
    web502:/)
      printf '%s\n' 502 - '502 Bad Gateway'
      ;;
    redir:/)
      printf '%s\n' 302 "https://test.ernemo.ir/login" ''
      ;;
    redir:/login)
      printf '%s\n' 200 - '<h1>ورود</h1><script src="/_next/static/site.js"></script>'
      ;;
    badredir:/)
      printf '%s\n' 302 "http://test.ernemo.ir/login" ''
      ;;
    asset400:/_next/static/site.js)
      printf '%s\n' 400 - 'build mismatch'
      ;;
    loopback:/_next/static/site.js)
      printf '%s\n' 200 - 'let a=r(1).env.NEXT_PUBLIC_API_BASE||"http://127.0.0.1:8741";'
      ;;
    foreign:/_next/static/site.js)
      printf '%s\n' 200 - 'let a="https://other.example.test";'
      ;;
    *:/_next/static/site.js)
      printf '%s\n' 200 - 'let a="https://test.ernemo.ir";'
      ;;
    *)
      printf '%s\n' 404 - 'missing'
      ;;
  esac
}
pp_domain_cors_ok() { [[ "$PP_HTTP_CASE" != cors ]]; }
probe_case() {
  local status=0
  PP_HTTP_CASE="$1"
  rm -f "$work/fetch-n"
  set +e
  pp_domain_https_probe test.ernemo.ir >/dev/null
  status=$?
  set -e
  printf '%s\n' "$status"
}
[[ "$(probe_case ok)" == "0" ]] || fail "a healthy API and login page were rejected"
[[ "$(probe_case badjson)" != "0" ]] || fail "a 200 response without ok true was accepted"
[[ "$(probe_case api500)" != "0" ]] || fail "API HTTP 500 was accepted"
[[ "$(probe_case web502)" != "0" ]] || fail "a frontend HTTP 502 was accepted"
[[ "$(probe_case redir)" == "0" ]] || fail "a valid HTTPS login redirect was rejected"
[[ "$(probe_case badredir)" != "0" ]] || fail "an HTTP login redirect was accepted"
[[ "$(probe_case slow)" == "0" ]] || fail "a service that became ready on retry was rejected"
[[ "$(probe_case asset400)" != "0" ]] || fail "an HTTP 400 JavaScript asset was accepted"
[[ "$(probe_case cors)" != "0" ]] || fail "a failed CORS check was accepted"
[[ "$(probe_case loopback)" != "0" ]] || fail "login JavaScript with the loopback API base was accepted"
[[ "$(probe_case foreign)" != "0" ]] || fail "login JavaScript for another origin was accepted"
ok "https probe"

printf 'previous-site\n' > "$PP_NGINX_SITE"
pp_domain_nginx_test() { return 0; }
pp_domain_nginx_reload() { return 0; }
pp_domain_acme_probe() { return 0; }
pp_domain_issue_cert() { return 0; }
pp_domain_build_web() { return 0; }
pp_domain_bundle_ok() { return 0; }
pp_domain_cert_files_present() { return 0; }
pp_env_value() { printf 'http://127.0.0.1:3741\n'; }
pp_domain_public_urls() { :; }
pp_domain_restore_urls() { :; }
pp_domain_https_probe() { return 1; }
app_running() { return 0; }
restart_app() { :; }
if pp_domain_activate test.ernemo.ir; then fail "a failed HTTPS probe was treated as success"; fi
grep -q 'previous-site' "$PP_NGINX_SITE" || fail "a failed HTTPS probe did not restore the previous site"
grep -q 'other-site' "$work/sites-available/other.conf" || fail "a failed HTTPS probe changed another site"
ok "https probe rollback"
rm -rf "$work" "$empty"
printf 'feature checks passed\n'
