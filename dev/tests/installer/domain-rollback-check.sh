#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
# shellcheck disable=SC1091
source "$repo/scripts/domain.sh"

fail() { printf 'FAIL %s: %s\n' "$case_name" "$*" >&2; exit 1; }

site_phase() {
  if [[ -f "$PP_NGINX_SITE" ]] && grep -Fq 'server_name console.example.com;' "$PP_NGINX_SITE"; then
    if grep -Fq 'ssl_certificate ' "$PP_NGINX_SITE"; then
      printf 'https'
    else
      printf 'acme'
    fi
  else
    printf 'previous'
  fi
}

# These mocks record the configuration read by nginx and track the active one.
nginx() {
  local phase
  [[ "$*" == -t ]] || fail "unexpected nginx $*"
  phase="$(site_phase)"
  printf 'test %s\n' "$phase" >> "$events"
  [[ "$phase" != "$invalid_phase" ]]
}

systemctl() {
  local phase
  case "$*" in
    'is-active --quiet nginx') [[ "$active" == 1 ]] || return 3 ;;
    'reload nginx'|'enable --now nginx')
      phase="$(site_phase)"
      printf '%s %s\n' "$1" "$phase" >> "$events"
      [[ "$phase" != "$reload_failure" ]] || return 1
      active=1
      printf '%s\n' "$phase" > "$active_config"
      ;;
    *) fail "unexpected systemctl $*" ;;
  esac
}

pp_domain_cert_covers() { return 1; }
pp_domain_acme_probe() { [[ "$failure" != acme ]]; }
pp_domain_issue_cert() { [[ "$failure" != certificate ]]; }
pp_domain_cert_files_present() { return 0; }
pp_env_value() { cat "$1"; }
pp_set_env_key() { printf '%s\n' "$3" > "$1"; }
eval "pp_domain_real_build_web() $(declare -f pp_domain_build_web | tail -n +2)"
# The env case runs the real build helper. The mocked web.env holds a bare URL,
# which pp_load_env_file rejects before npm can run.
pp_domain_build_web() {
  case "$failure" in
    env) pp_domain_real_build_web "$@" ;;
    build) printf 'new build\n' > "$PP_ROOT/.next/marker"; return 1 ;;
    *)
      [[ "$1" == https://console.example.com ]] || fail "unexpected build origin $1"
      printf 'new build\n' > "$PP_ROOT/.next/marker"
      ;;
  esac
}
npm() { printf 'npm\n' >> "$PP_ROOT/npm-ran"; return 1; }
pp_domain_bundle_ok() { [[ "$failure" != bundle ]]; }
pp_domain_https_probe() { [[ "$failure" != https ]]; }
app_running() { return 0; }
restart_app() {
  printf 'restart\n' >> "$PP_ROOT/restarts"
  [[ "$failure" != restart || "$(wc -l < "$PP_ROOT/restarts")" -gt 1 ]]
}
pp_domain_install_renew_hook() { fail 'unexpected renewal hook installation'; }

check_rollback() (
  local case_name="$1" previous="$2" failure="$3" invalid_phase="$4" reload_failure="$5" active="$6"
  local expected_events="$7" expected_active="$8" expected_error="$9" expected_restarts="${10:-}" status
  PP_ROOT="$work/$case_name"
  PP_NGINX_SITE="$PP_ROOT/sites-available/partnerconsole.conf"
  PP_NGINX_LINK="$PP_ROOT/sites-enabled/partnerconsole.conf"
  PP_ACME_ROOT="$PP_ROOT/acme"
  PP_CERT_FULLCHAIN="$PP_ROOT/fullchain.pem"
  PP_CERT_PRIVKEY="$PP_ROOT/privkey.pem"
  unset PP_DOMAIN_STATE
  events="$PP_ROOT/events"
  active_config="$PP_ROOT/active"
  mkdir -p "$PP_ROOT"/{sites-available,sites-enabled,config,.next,scripts}
  cp "$repo/scripts/common.sh" "$PP_ROOT/scripts/common.sh"
  printf '{}\n' > "$PP_ROOT/package.json"
  printf 'other site\n' > "$PP_ROOT/sites-available/other.conf"
  ln -s -- ../sites-available/other.conf "$PP_ROOT/sites-enabled/other.conf"
  printf '%s\n' 'https://old.example.com' > "$PP_ROOT/config/api.env"
  printf '%s\n' 'http://127.0.0.1:8741' > "$PP_ROOT/config/web.env"
  printf 'old build\n' > "$PP_ROOT/.next/marker"
  if [[ "$active" == 1 ]]; then printf 'previous\n' > "$active_config"; fi
  case "$previous" in
    symlink)
      printf 'old site\n' > "$PP_NGINX_SITE"
      ln -s -- ../sites-available/partnerconsole.conf "$PP_NGINX_LINK"
      ;;
    file)
      ln -s -- other.conf "$PP_NGINX_SITE"
      printf 'old enabled site\n' > "$PP_NGINX_LINK"
      ;;
    absent) ;;
  esac

  # Keep errexit enabled inside activation even while collecting its status.
  set +e
  (set -e; pp_domain_activate console.example.com) > "$PP_ROOT/output" 2>&1
  status=$?
  set -e
  [[ "$status" == 1 ]] || fail "unexpected status $status"
  grep -Fq "$expected_error" "$PP_ROOT/output" || fail "original error missing: $(cat "$PP_ROOT/output")"
  [[ "$(cat "$events")" == "$expected_events" ]] || fail "unexpected nginx events: $(cat "$events")"
  if [[ "$expected_active" == inactive ]]; then
    [[ ! -e "$active_config" ]] || fail 'nginx was started'
  else
    [[ "$(cat "$active_config")" == "$expected_active" ]] || fail 'unexpected active configuration'
  fi
  if [[ "$invalid_phase" == previous ]]; then
    grep -Fq 'nginx -t failed after the rollback.' "$PP_ROOT/output" || fail 'rollback validation error missing'
  fi
  if [[ "$reload_failure" == previous ]]; then
    grep -Fq 'Nginx could not reload the restored configuration.' "$PP_ROOT/output" || fail 'rollback reload error missing'
  fi
  case "$previous" in
    symlink)
      [[ "$(cat "$PP_NGINX_SITE")" == 'old site' ]] || fail 'previous site missing'
      [[ "$(readlink "$PP_NGINX_LINK")" == ../sites-available/partnerconsole.conf ]] || fail 'original relative link missing'
      ;;
    file)
      [[ "$(readlink "$PP_NGINX_SITE")" == other.conf ]] || fail 'original site symlink missing'
      [[ ! -L "$PP_NGINX_LINK" && "$(cat "$PP_NGINX_LINK")" == 'old enabled site' ]] || fail 'original enabled file missing'
      ;;
    absent)
      [[ ! -e "$PP_NGINX_SITE" && ! -L "$PP_NGINX_SITE" && ! -e "$PP_NGINX_LINK" && ! -L "$PP_NGINX_LINK" ]] || fail 'new site or link survived'
      ;;
  esac
  [[ "$(cat "$PP_ROOT/sites-available/other.conf")" == 'other site' ]] || fail 'another site changed'
  [[ "$(readlink "$PP_ROOT/sites-enabled/other.conf")" == ../sites-available/other.conf ]] || fail 'another link changed'
  [[ "$(cat "$PP_ROOT/config/api.env")" == 'https://old.example.com' ]] || fail 'previous CORS origin missing'
  [[ "$(cat "$PP_ROOT/config/web.env")" == 'http://127.0.0.1:8741' ]] || fail 'previous API base missing'
  [[ "$(cat "$PP_ROOT/.next/marker")" == 'old build' ]] || fail 'previous web build missing'
  [[ ! -e "$PP_ROOT/npm-ran" ]] || fail 'npm ran with an unreadable web.env'
  if [[ -n "$expected_restarts" ]]; then
    [[ "$(cat "$PP_ROOT/restarts" 2>/dev/null | wc -l)" == "$expected_restarts" ]] || fail "expected $expected_restarts restarts"
  fi
  printf 'ok nginx rollback %s\n' "$case_name"
)

check_rollback acme-failure symlink acme '' '' 1 \
  $'test acme\nreload acme\ntest previous\nreload previous' previous 'The ACME challenge path was not reachable.'
check_rollback no-previous-site absent certificate '' '' 1 \
  $'test acme\nreload acme\ntest previous\nreload previous' previous 'The certificate was not issued.'
check_rollback https-failure file https '' '' 1 \
  $'test acme\nreload acme\ntest https\nreload https\ntest previous\nreload previous' previous 'HTTPS health failed after restart.'
check_rollback invalid-restored-config symlink acme previous '' 1 \
  $'test acme\nreload acme\ntest previous' acme 'The ACME challenge path was not reachable.'
check_rollback reload-failure symlink acme '' previous 1 \
  $'test acme\nreload acme\ntest previous\nreload previous' acme 'The ACME challenge path was not reachable.'
check_rollback inactive-nginx absent none acme '' 0 \
  $'test acme\ntest previous' inactive 'nginx -t failed for the ACME site.'
check_rollback nginx-activated-for-acme absent acme '' '' 0 \
  $'test acme\nenable acme\ntest previous\nreload previous' previous 'The ACME challenge path was not reachable.'
check_rollback web-env-failure symlink env '' '' 1 \
  $'test acme\nreload acme\ntest previous\nreload previous' previous 'config/web.env could not be read.' 0
check_rollback build-failure symlink build '' '' 1 \
  $'test acme\nreload acme\ntest previous\nreload previous' previous 'The web build failed.' 0
check_rollback bundle-failure file bundle '' '' 1 \
  $'test acme\nreload acme\ntest previous\nreload previous' previous \
  'The sign-in JavaScript does not use https://console.example.com as its API origin.' 0
check_rollback restart-failure symlink restart '' '' 1 \
  $'test acme\nreload acme\ntest https\nreload https\ntest previous\nreload previous' previous \
  'The application restart failed.' 2
check_rollback https-failure-restarts-previous absent https '' '' 1 \
  $'test acme\nreload acme\ntest https\nreload https\ntest previous\nreload previous' previous \
  'HTTPS health failed after restart.' 2
