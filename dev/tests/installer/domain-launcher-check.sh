#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
installer="${1:-$repo/partnerconsole.sh}"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

export PARTNERCONSOLE_LIBRARY=1
# shellcheck disable=SC1090
source "$installer"

unset PP_ROOT PP_DOMAIN_LIBRARY PARTNERCONSOLE_ROOT
resolve_root
# shellcheck disable=SC2153 # Set by resolve_root in the sourced installer.
[[ "$APP_ROOT" == /srv/partnerconsole ]] || fail 'default install root'
export PARTNERCONSOLE_ROOT="$work/custom/"
resolve_root
[[ "$APP_ROOT" == "$work/custom" ]] || fail 'custom install root'

assert_runtime_locked() { :; }
mkdir -p "$APP_ROOT/scripts"
cat > "$APP_ROOT/scripts/domain.sh" <<'EOF'
[[ "$PP_DOMAIN_LIBRARY" == 1 && "$PP_ROOT" == "$APP_ROOT" ]] || exit 96
pp_domain_setup() {
  printf '%s\n' "$PP_ROOT" > "$APP_ROOT/domain-called"
}
EOF

domain_app
[[ "$(cat "$APP_ROOT/domain-called")" == "$APP_ROOT" ]] || fail 'domain setup did not receive APP_ROOT'
[[ ! -v PP_ROOT && ! -v PP_DOMAIN_LIBRARY ]] || fail 'domain variables leaked'
PP_ROOT=caller-root PP_DOMAIN_LIBRARY=caller-mode
domain_app
[[ "$PP_ROOT" == caller-root && "$PP_DOMAIN_LIBRARY" == caller-mode ]] || fail 'domain changed caller variables'
unset PP_ROOT PP_DOMAIN_LIBRARY
printf 'ok launcher domain scope and default/custom roots\n'

rm -- "$APP_ROOT/domain-called"
run_choice domain
[[ "$(cat "$APP_ROOT/domain-called")" == "$APP_ROOT" ]] || fail 'run_choice domain did not reach setup'

rm -- "$APP_ROOT/domain-called"
(
  menu_input=(9 '' 0)
  # shellcheck disable=SC2329 # Called by the sourced menu_loop.
  tty_read() {
    printf -v "${!#}" '%s' "${menu_input[0]}"
    menu_input=("${menu_input[@]:1}")
  }
  menu_loop
) > "$work/menu"
grep -Fq '9) Domain & SSL' "$work/menu" || fail 'menu label missing'
[[ "$(cat "$APP_ROOT/domain-called")" == "$APP_ROOT" ]] || fail 'menu option 9 did not reach setup'

# Exercise the unmodified executable entry point with stubbed ownership and
# allow its installed root to be the temporary fixture.
cp "$installer" "$APP_ROOT/partnerconsole.sh"
mkdir -p "$APP_ROOT/.release"
touch "$APP_ROOT/.release/current" "$APP_ROOT"/{install,start,stop,restart}.sh \
  "$APP_ROOT/scripts"/{common,backup,bootstrap}.sh
# shellcheck disable=SC2329 # Imported by the executable installer.
id() { [[ "$*" == -u ]] && printf '0\n'; }
# shellcheck disable=SC2329 # Imported by the executable installer.
stat() {
  case "$2" in
    %u) printf '0\n' ;;
    %a) printf '755\n' ;;
    *) return 1 ;;
  esac
}
export -f id stat
cat > "$work/bash-env" <<'EOF'
trap '
  if [[ "$BASH_COMMAND" == validate_root ]]; then
    validate_root() { [[ "$APP_ROOT" == "$PARTNERCONSOLE_ROOT" ]]; }
    trap - DEBUG
  fi
' DEBUG
EOF
rm -- "$APP_ROOT/domain-called"
PARTNERCONSOLE_ROOT="$APP_ROOT" PARTNERCONSOLE_LIBRARY=0 BASH_ENV="$work/bash-env" bash "$APP_ROOT/partnerconsole.sh" domain
[[ "$(cat "$APP_ROOT/domain-called")" == "$APP_ROOT" ]] || fail 'executable domain command did not reach setup'
unset -f id stat
printf 'ok domain dispatch through run_choice, menu option 9 and executable entry point\n'

# Append a test entry point to the real domain library in the custom install.
# The interactive driver is replaced here to exercise real helpers without a TTY.
unset PP_NGINX_SITE PP_NGINX_LINK PP_ACME_ROOT PP_CERT_FULLCHAIN PP_CERT_PRIVKEY PP_RENEW_HOOK
cp "$repo/scripts/domain.sh" "$APP_ROOT/scripts/domain.sh"
cat >> "$APP_ROOT/scripts/domain.sh" <<'EOF'
pp_domain_setup() {
  [[ "$PP_ROOT" == "$APP_ROOT" && "$PP_DOMAIN_LIBRARY" == 1 ]] || return 1
  pp_domain_owned_path "$PP_NGINX_SITE" && pp_domain_owned_path "$PP_NGINX_LINK" &&
    pp_domain_owned_path "$PP_RENEW_HOOK" || return 1
  pp_nginx_http_config console.example.com > "$PP_ROOT/http.conf"
  pp_nginx_https_config console.example.com > "$PP_ROOT/https.conf"
}
EOF
domain_app
grep -Fq 'root /var/lib/partnerconsole/acme;' "$APP_ROOT/http.conf" || fail 'ACME root was not initialized'
grep -Fq 'ssl_certificate /etc/letsencrypt/live/partnerconsole/fullchain.pem;' "$APP_ROOT/https.conf" || fail 'certificate path was not initialized'
grep -Fq 'ssl_certificate_key /etc/letsencrypt/live/partnerconsole/privkey.pem;' "$APP_ROOT/https.conf" || fail 'private key path was not initialized'
[[ ! -v PP_ROOT && ! -v PP_DOMAIN_LIBRARY ]] || fail 'real domain library leaked launcher variables'
printf 'ok real domain library loaded by launcher and rendered HTTP/HTTPS configuration\n'
