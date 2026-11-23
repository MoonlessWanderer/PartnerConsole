#!/usr/bin/env bash
# Run as root in disposable Linux with nginx, curl and su installed.
# Starts a private Nginx on loopback:18080 and sends no ACME requests.
set -euo pipefail
umask 077

[[ "$(uname -s)" == Linux && "$EUID" == 0 ]] || { echo 'Requires disposable Linux as root.' >&2; exit 1; }
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
baseline="${1:-}"
# shellcheck disable=SC1091
source "$repo/scripts/domain.sh"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
work="$(mktemp -d)"
chmod 755 "$work"
port=18080
worker=nobody
worker_group="$(id -gn "$worker")"
domain=console.example.test
PP_ROOT="$work/app"
PP_ACME_ROOT="$work/var/lib/partnerconsole/acme"
PP_NGINX_SITE="$work/available/partnerconsole.conf"
PP_NGINX_LINK="$work/enabled/partnerconsole.conf"
PP_CERT_FULLCHAIN="$work/no-certificate.pem"
PP_CERT_PRIVKEY="$work/no-key.pem"
challenge="$PP_ACME_ROOT/.well-known/acme-challenge"
mkdir -p "$PP_ROOT" "$work/available" "$work/enabled" "$work/logs"
access="$work/access"
trace="$work/probes"
master=''
delayed_reload=''
worker_state() {
  [[ -n "$master" ]] || return 0
  ps -p "$master" -o pid,ppid,stat,args
  ps --ppid "$master" -o pid,ppid,stat,args || true
}
cleanup() {
  local status=$? log
  if [[ -n "$delayed_reload" ]]; then
    kill "$delayed_reload" 2>/dev/null || true
    wait "$delayed_reload" 2>/dev/null || true
  fi
  if [[ -n "$master" ]]; then
    if [[ "$status" != 0 ]]; then worker_state >&2 || true; fi
    kill -TERM "$master" 2>/dev/null || true
    wait "$master" 2>/dev/null || true
  fi
  for log in "$trace" "$access" "$work/error"; do
    if [[ -f "$log" ]]; then
      printf '\n--- %s ---\n' "$(basename "$log")" >&2
      cat "$log" >&2
    fi
  done
  rm -rf -- "$work"
  exit "$status"
}
trap cleanup EXIT

nginx() { command nginx -p "$work/" -c "$work/nginx.conf" "$@"; }
systemctl() {
  case "$*" in
    'is-active --quiet nginx') [[ -n "$master" ]] && kill -0 "$master" ;;
    'reload nginx')
      local status=0
      printf '%s before reload\n' "$(date -u +%FT%T.%NZ)" >&2
      worker_state >&2
      nginx -s reload || status=$?
      printf '%s reload exit=%s\n' "$(date -u +%FT%T.%NZ)" "$status" >&2
      worker_state >&2
      return "$status"
      ;;
    *) fail "unexpected systemctl action: $*" ;;
  esac
}

cat > "$work/nginx.conf" <<EOF
user $worker $worker_group;
worker_processes 1;
daemon off;
pid $work/nginx.pid;
error_log $work/error notice;
events { worker_connections 128; }
http {
    log_format evidence '\$host \$server_name \$status \$request_uri pid=\$pid time=\$msec';
    access_log $access evidence;
    client_body_temp_path $work/client;
    proxy_temp_path $work/proxy;
    fastcgi_temp_path $work/fastcgi;
    uwsgi_temp_path $work/uwsgi;
    scgi_temp_path $work/scgi;
    server {
        listen 127.0.0.1:$port default_server;
        server_name default.example.test;
        location ^~ /.well-known/acme-challenge/ { return 404; }
        location / { return 200 'default site'; }
    }
    include $work/enabled/*.conf;
}
EOF
render() {
  sed -e "s/listen 80;/listen 127.0.0.1:$port;/" -e "s/listen \\[::\\]:80;/listen [::1]:$port;/"
}
request() {
  command curl --disable --noproxy '*' --silent --max-time 2 --output "$work/body" \
    --write-out '%{http_code}' -H 'Connection: close' -H "Host: $1" "http://127.0.0.1:$port$2"
}
wait_for_status() {
  local host="$1" path="$2" wanted="$3" _
  for _ in {1..30}; do
    if [[ "$(request "$host" "$path")" == "$wanted" ]]; then return 0; fi
    sleep 0.1
  done
  return 1
}

wait_for_worker_exit() {
  local attempt pid active
  for attempt in {1..30}; do
    active=0
    for pid in "$@"; do
      if kill -0 "$pid" 2>/dev/null; then active=1; fi
    done
    if [[ "$active" == 0 ]]; then return 0; fi
    sleep 0.1
  done
  return 1
}

pp_domain_prepare_acme_webroot
for dir in "$PP_ACME_ROOT" "$PP_ACME_ROOT/.well-known" "$challenge"; do
  [[ "$(stat -c '%u:%a' "$dir")" == 0:755 ]] || fail 'webroot ownership or mode'
  su -s /bin/sh "$worker" -c "test -x '$dir'" || fail 'worker cannot traverse webroot'
done
printf 'private fixture\n' > "$PP_ROOT/api.env"
if su -s /bin/sh "$worker" -c "test -r '$PP_ROOT/api.env'"; then fail 'worker read private data'; fi
printf 'ok Linux parent traversal and private file permissions\n'
ln -s "$PP_ACME_ROOT" "$work/acme-symlink"
if PP_ACME_ROOT="$work/acme-symlink" pp_domain_prepare_acme_webroot; then fail 'symlink webroot accepted'; fi
if PP_ACME_ROOT="$PP_ACME_ROOT/../acme" pp_domain_prepare_acme_webroot; then fail 'webroot traversal accepted'; fi
mkdir -m 777 "$work/writable-acme"
if PP_ACME_ROOT="$work/writable-acme" pp_domain_prepare_acme_webroot; then fail 'writable webroot accepted'; fi

# shellcheck disable=SC2218
pp_nginx_http_config "$domain" | render > "$PP_NGINX_SITE"
ln -s ../available/partnerconsole.conf "$PP_NGINX_LINK"
nginx -t
command nginx -p "$work/" -c "$work/nginx.conf" &
master=$!
wait_for_status "$domain" / 404 || fail 'private Nginx did not start with the target vhost'

deny_probe=0
inspect_probe=1
curl() {
  local -a arguments=("$@")
  local last=$(($# - 1)) token output='' body='' code status=0 i
  arguments[last]="${arguments[last]/127.0.0.1\//127.0.0.1:$port\/}"
  token="${arguments[last]##*/}"
  if [[ "$inspect_probe" == 1 ]]; then
    [[ "$(stat -c '%u:%a' "$challenge/$token")" == 0:644 ]] || fail 'live probe is not root:0644'
    su -s /bin/sh "$worker" -c "test -r '$challenge/$token'" || fail 'worker cannot read live probe'
  fi
  if [[ "$deny_probe" == 1 ]]; then chmod 600 "$challenge/$token"; fi
  for ((i=0; i<last; i++)); do
    if [[ "${arguments[i]}" == --output ]]; then output="${arguments[i+1]}"; fi
  done
  if [[ -z "$output" ]]; then
    command curl -H 'Connection: close' "${arguments[@]}"
    return
  fi
  code="$(command curl -H 'Connection: close' "${arguments[@]}")" || status=$?
  if [[ -f "$output" ]]; then body="$(head -c 256 "$output")"; fi
  printf '%s host=%s url=%s status=%s curl_exit=%s body=%q\n' "$(date -u +%FT%T.%NZ)" \
    "$domain" "${arguments[last]}" "$code" "$status" "$body" >> "$trace"
  printf '%s' "$code"
  return "$status"
}

rc4_probe() (
  # shellcheck disable=SC1090
  source "$baseline"
  inspect_probe=0
  pp_domain_acme_probe "$domain"
)

if [[ -n "$baseline" ]]; then
  if rc4_probe; then fail 'RC4 passed the unreadable probe'; fi
  [[ "$(stat -c '%u:%a' "$challenge/pc-acme-probe")" == 0:600 ]] || fail 'unexpected RC4 file mode'
  if su -s /bin/sh "$worker" -c "test -r '$challenge/pc-acme-probe'"; then fail 'worker read RC4 probe'; fi
  grep -Fq "$domain $domain 403 /.well-known/acme-challenge/pc-acme-probe" "$access" || fail 'RC4 permission response'
  printf 'ok RC4 reproduces root:0600 and real HTTP 403 in the correct vhost\n'
fi

pp_domain_acme_probe "$domain"
grep -Fq "$domain $domain 200 /.well-known/acme-challenge/pc-acme-probe." "$access" || fail 'target vhost did not serve probe'
[[ "$(umask)" == 0077 ]] || fail 'installer umask changed'
shopt -s nullglob
probes=("$challenge"/pc-acme-probe.*)
[[ "${#probes[@]}" == 0 ]] || fail 'successful probe file survived cleanup'
printf 'ok real Nginx worker, probe root:0644, matching body and cleanup\n'

certbot_filename=dGVzdC10b2tlbi0xMjM0NTY
certbot_body="$certbot_filename.cHVibGljLXRlc3QtYXV0aG9yaXphdGlvbg"
certbot_path="/.well-known/acme-challenge/$certbot_filename"
( umask 022; printf '%s' "$certbot_body" > "$challenge/$certbot_filename" )
[[ "$(request "$domain" "$certbot_path")" == 200 && "$(cat "$work/body")" == "$certbot_body" ]] || fail 'Certbot-like challenge'
[[ "$(request wrong.example.test "$certbot_path")" == 404 ]] || fail 'wrong host saw the challenge'
[[ "$(request wrong.example.test /)" == 200 && "$(cat "$work/body")" == 'default site' ]] || fail 'default site fixture'
[[ "$(request "$domain" /.well-known/acme-challenge/missing)" == 404 ]] || fail 'missing challenge'
printf 'unreadable\n' > "$challenge/unreadable"
[[ "$(request "$domain" /.well-known/acme-challenge/unreadable)" == 403 ]] || fail 'unreadable challenge'
printf 'ok Certbot-like challenge, wrong host, default site, missing file and permission denied\n'

# A response from a new worker does not prove the old workers have stopped
# accepting connections. Retire the target-vhost generation before probing 404.
mapfile -t old_workers < <(ps --ppid "$master" -o pid= | awk '{print $1}')
[[ "${#old_workers[@]}" -gt 0 ]] || fail 'no target-vhost worker before reload'
rm -- "$PP_NGINX_LINK"
pp_domain_nginx_test
pp_domain_nginx_reload
wait_for_worker_exit "${old_workers[@]}" || fail 'old target-vhost workers survived reload'
worker_state >&2
wait_for_status "$domain" / 200 && [[ "$(cat "$work/body")" == 'default site' ]] ||
  fail 'default vhost did not become active'
if [[ -n "$baseline" ]]; then
  if rc4_probe; then fail 'RC4 passed against the default vhost'; fi
  grep -Fq "$domain default.example.test 404 /.well-known/acme-challenge/pc-acme-probe pid=" "$access" ||
    fail 'RC4 routing response'
  printf 'ok RC4 reproduces HTTP 404 while the default vhost serves the request\n'
fi
ln -s ../available/partnerconsole.conf "$PP_NGINX_LINK"
pp_domain_nginx_test
(
  for attempt in {1..30}; do
    if grep -Fq "$domain default.example.test 404 /.well-known/acme-challenge/pc-acme-probe." "$access"; then
      pp_domain_nginx_reload
      exit
    fi
    sleep 0.1
  done
  exit 1
) &
delayed_reload=$!
pp_domain_acme_probe "$domain" || fail 'probe did not wait for real reload'
wait "$delayed_reload" || fail 'delayed reload did not run'
delayed_reload=''
awk -v host="$domain" '
  $1 == host && $4 ~ /^\/[.]well-known\/acme-challenge\/pc-acme-probe[.]/ {
    if ($2 == "default.example.test" && $3 == 404) before[$4] = $5
    if ($2 == host && $3 == 200 && ($4 in before) && before[$4] != $5) {
      print "ok same probe changed worker from " before[$4] " to " $5
      passed = 1
    }
  }
  END { exit !passed }
' "$access" || fail 'same probe did not receive 404 and then 200 from a different worker'
printf 'ok real delayed reload, first request hit the default vhost and retry succeeded\n'

# shellcheck disable=SC2218
pp_nginx_http_config previous.example.test | render > "$PP_NGINX_SITE"
cp "$PP_NGINX_SITE" "$work/previous.conf"
printf 'unchanged\n' > "$work/available/unrelated.conf"
pp_domain_nginx_test
pp_domain_nginx_reload
wait_for_status previous.example.test / 404 || fail 'previous vhost did not become active'
# Keep the installer template and adapt its listener to the private port.
eval "$(declare -f pp_nginx_http_config | sed '1s/pp_nginx_http_config/original_http_config/')"
pp_nginx_http_config() { original_http_config "$1" | render; }
pp_domain_issue_cert() { fail 'Certbot ran after a failed probe'; }
app_running() { return 1; }
restart_app() { fail 'the stopped app was restarted'; }
deny_probe=1
inspect_probe=0
if pp_domain_activate "$domain" > "$work/activation" 2>&1; then fail 'permanent failure passed'; fi
if ! grep -Fq 'ACME probe failed after 5 attempts: HTTP 403' "$work/activation"; then
  cat "$work/activation" >&2
  fail 'permission failure diagnosis'
fi
grep -Fq 'The ACME challenge path was not reachable.' "$work/activation" || fail 'activation did not fail at ACME'
cmp -s "$PP_NGINX_SITE" "$work/previous.conf" || fail 'previous site was not restored'
[[ "$(readlink "$PP_NGINX_LINK")" == ../available/partnerconsole.conf ]] || fail 'previous symlink was not restored'
[[ "$(cat "$work/available/unrelated.conf")" == unchanged ]] || fail 'unrelated site changed'
wait_for_status previous.example.test / 404 || fail 'previous config was not restored to live Nginx'
wait_for_status "$domain" / 200 || fail 'failed ACME vhost is still active'
probes=("$challenge"/pc-acme-probe.*)
[[ "${#probes[@]}" == 0 && -f "$challenge/$certbot_filename" && -f "$challenge/unreadable" ]] || fail 'rollback cleanup touched other tokens'
printf 'ok real permanent failure, five attempts, previous site and symlink reloaded, other tokens intact\n'
