#!/usr/bin/env bash
# Domain and TLS for one HTTPS name. Nginx and certbot are installed only after
# an explicit yes, and never by a normal install or update. The application
# itself still listens only on 127.0.0.1 and runs as ppv1.

PP_NGINX_SITE="${PP_NGINX_SITE:-/etc/nginx/sites-available/partnerconsole.conf}"
PP_NGINX_LINK="${PP_NGINX_LINK:-/etc/nginx/sites-enabled/partnerconsole.conf}"
PP_SYSTEMD_UNIT="${PP_SYSTEMD_UNIT:-/etc/systemd/system/partnerconsole.service}"
PP_ACME_ROOT="${PP_ACME_ROOT:-/var/lib/partnerconsole/acme}"
PP_CERT_FULLCHAIN="${PP_CERT_FULLCHAIN:-/etc/letsencrypt/live/partnerconsole/fullchain.pem}"
PP_CERT_PRIVKEY="${PP_CERT_PRIVKEY:-/etc/letsencrypt/live/partnerconsole/privkey.pem}"
PP_RENEW_HOOK="${PP_RENEW_HOOK:-/etc/letsencrypt/renewal-hooks/deploy/partnerconsole}"

pp_domain_valid() {
  local name="${1,,}"
  [[ ${#name} -le 253 && "$name" != *"*"* && "$name" != *..* ]] || return 1
  [[ "$name" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$ ]]
}

pp_domain_ipv4_mapped() {
  [[ "$1" == ::ffff:* ]]
}

pp_domain_dns_ok() {
  local want4="$1" want6="${2:-}" records4="$3" records6="${4:-}" line found4=0 any6=0
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    pp_domain_ipv4_mapped "$line" && return 1
    [[ "$line" == "$want4" ]] && found4=1
  done <<<"$records4"
  [[ "$found4" == "1" ]] || return 1
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    [[ "$line" == "$want4" ]] || return 1
  done <<<"$records4"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    pp_domain_ipv4_mapped "$line" && return 1
    any6=1
    [[ -n "$want6" && "$line" == "$want6" ]] || return 1
  done <<<"$records6"
  [[ "$any6" == "0" || -n "$want6" ]]
}

pp_domain_server_ipv4() {
  ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}'
}

pp_domain_server_ipv6() {
  local addr
  addr="$(ip -6 route get 2001:4860:4860::8888 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  [[ -n "$addr" ]] || return 1
  pp_domain_ipv4_mapped "$addr" && return 1
  [[ "$addr" == fe80:* || "$addr" == ::1 ]] && return 1
  printf '%s\n' "$addr"
}

pp_domain_dig() {
  local type="$1" name="$2"
  if ! command -v dig >/dev/null 2>&1; then
    echo "dig is not installed." >&2
    return 5
  fi
  dig +time=3 +tries=1 "$type" "$name"
}

pp_domain_query() {
  local type="$1" name="$2" output status_code=0 dig_status records line
  output="$(pp_domain_dig "$type" "$name")" || status_code=$?
  if [[ "$status_code" -eq 5 ]]; then
    echo "The DNS tool dig is not installed, so ${name} was not looked up." >&2
    return 5
  fi
  if [[ "$status_code" -eq 9 ]]; then
    echo "DNS lookup for ${type} ${name} timed out. That is not an empty answer." >&2
    return 4
  fi
  dig_status="$(printf '%s\n' "$output" | sed -n 's/.*status: \([A-Z][A-Z]*\).*/\1/p' | head -n 1)"
  if [[ -z "$dig_status" ]]; then
    echo "DNS lookup for ${type} ${name} did not return a DNS status. That is not an empty answer." >&2
    return 4
  fi
  case "$dig_status" in
    NOERROR) ;;
    NXDOMAIN)
      echo "DNS has no such name ${name}." >&2
      return 2
      ;;
    SERVFAIL|REFUSED)
      echo "DNS server failed for ${name} (${dig_status})." >&2
      return 3
      ;;
    *)
      echo "DNS lookup for ${name} returned ${dig_status}." >&2
      return 4
      ;;
  esac
  records="$(printf '%s\n' "$output" | awk -v kind="$type" '$4==kind {print $NF}')"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    if pp_domain_ipv4_mapped "$line"; then
      echo "DNS returned ${line}, which is an IPv4-mapped address and not a real ${type} record." >&2
      return 1
    fi
  done <<<"$records"
  printf '%s\n' "$records"
}

pp_domain_ensure_dig() {
  if command -v dig >/dev/null 2>&1; then
    return 0
  fi
  echo "Domain setup needs the Ubuntu package bind9-dnsutils so DNS A and AAAA answers can be read separately."
  echo "It is installed only from the Ubuntu repositories, and only after this confirmation."
  if ! pp_domain_confirm "Install bind9-dnsutils now? [y/N] "; then
    echo "The DNS tool was not installed. No domain configuration was changed." >&2
    return 5
  fi
  if ! pp_domain_apt update || ! pp_domain_apt install -y bind9-dnsutils; then
    echo "Installing bind9-dnsutils failed. No domain configuration was changed." >&2
    return 1
  fi
  command -v dig >/dev/null 2>&1
}

pp_domain_lookup() {
  local domain="$1" ipv4="$2" ipv6="${3:-}" rec4 rec6
  rec4="$(pp_domain_query A "$domain")" || return $?
  rec6="$(pp_domain_query AAAA "$domain")" || return $?
  pp_domain_dns_ok "$ipv4" "$ipv6" "$rec4" "$rec6"
}

pp_foreign_web_listener() {
  local line
  while IFS= read -r line; do
    [[ "$line" == *":80"* || "$line" == *":443"* ]] || continue
    [[ "$line" == *"nginx"* ]] && continue
    return 0
  done <<<"$1"
  return 1
}

pp_nginx_http_config() {
  local domain="$1"
  cat <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${domain};
    location ^~ /.well-known/acme-challenge/ {
        root ${PP_ACME_ROOT};
    }
    location / {
        return 404;
    }
}
EOF
}

pp_nginx_https_config() {
  local domain="$1"
  cat <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${domain};
    location ^~ /.well-known/acme-challenge/ {
        root ${PP_ACME_ROOT};
    }
    location / {
        return 301 https://\$host\$request_uri;
    }
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name ${domain};
    ssl_certificate ${PP_CERT_FULLCHAIN};
    ssl_certificate_key ${PP_CERT_PRIVKEY};
    location /api/ {
        # No URI on proxy_pass, so the /api prefix reaches FastAPI unchanged.
        proxy_pass http://127.0.0.1:8741;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    location / {
        proxy_pass http://127.0.0.1:3741;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}
EOF
}

pp_systemd_unit_text() {
  cat <<'EOF'
[Unit]
Description=Partner Console
After=network-online.target mysql.service
Wants=network-online.target

[Service]
# Boot start only. This unit does not watch API or Next.js and does not restart them after a crash.
# partnerconsole status reports the real processes. systemctl is-active does not.
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/partnerconsole start
ExecStop=/usr/local/bin/partnerconsole stop
User=root

[Install]
WantedBy=multi-user.target
EOF
}

pp_domain_renew_hook_text() {
  cat <<'EOF'
#!/bin/bash
# Reload nginx after this Partner Console certificate is renewed.
# Other certificates are ignored, and no site file is edited.
set -euo pipefail
if [[ -n "${RENEWED_LINEAGE:-}" && "$RENEWED_LINEAGE" != "/etc/letsencrypt/live/partnerconsole" ]]; then
  exit 0
fi
nginx -t
systemctl reload nginx
EOF
}

pp_domain_owned_path() {
  case "$1" in
    "$PP_NGINX_SITE"|"$PP_NGINX_LINK"|"$PP_SYSTEMD_UNIT"|"$PP_RENEW_HOOK") return 0 ;;
  esac
  return 1
}

pp_domain_restore_next() {
  local saved="$1"
  [[ -n "$PP_ROOT" && "$PP_ROOT" != "/" ]] || return 1
  rm -rf -- "$PP_ROOT/.next" || return 1
  if [[ -n "$saved" && -d "$saved" ]]; then
    mv -- "$saved" "$PP_ROOT/.next"
  fi
}

pp_domain_rollback() {
  local state="$1" line kind rest failed=0
  [[ -f "$state" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    kind="${line%% *}"
    rest="${line#* }"
    case "$kind" in
      save-site)
        cp -aT --remove-destination -- "$rest" "$PP_NGINX_SITE" || failed=1
        ;;
      save-link)
        ln -sfnT -- "$rest" "$PP_NGINX_LINK" || failed=1
        ;;
      save-next)
        pp_domain_restore_next "$rest" || failed=1
        ;;
      save-enabled)
        cp -aT --remove-destination -- "$rest" "$PP_NGINX_LINK" || failed=1
        ;;
      remove-next)
        pp_domain_restore_next "" || failed=1
        ;;
      remove)
        pp_domain_owned_path "$rest" || continue
        rm -f -- "$rest" || failed=1
        ;;
      *)
        pp_domain_owned_path "$line" || continue
        rm -f -- "$line" || failed=1
        ;;
    esac
  done < "$state" || return 1
  return "$failed"
}

pp_domain_remember_site() {
  local state="$1" backup target
  install -d -m 700 "$PP_ROOT/.release" || return 1
  if [[ -e "$PP_NGINX_SITE" || -L "$PP_NGINX_SITE" ]]; then
    backup="$PP_ROOT/.release/domain-site.previous"
    rm -rf -- "$backup" || return 1
    cp -a -- "$PP_NGINX_SITE" "$backup" || return 1
    printf 'save-site %s\n' "$backup" >> "$state" || return 1
  else
    printf 'remove %s\n' "$PP_NGINX_SITE" >> "$state" || return 1
  fi
  if [[ -L "$PP_NGINX_LINK" ]]; then
    target="$(readlink "$PP_NGINX_LINK")" || return 1
    printf 'save-link %s\n' "$target" >> "$state"
  elif [[ -e "$PP_NGINX_LINK" ]]; then
    backup="$PP_ROOT/.release/domain-link.previous"
    rm -rf -- "$backup" || return 1
    cp -a -- "$PP_NGINX_LINK" "$backup" || return 1
    printf 'save-enabled %s\n' "$backup" >> "$state"
  else
    printf 'remove %s\n' "$PP_NGINX_LINK" >> "$state"
  fi
}

pp_domain_save_next() {
  local state="$1" backup="$PP_ROOT/.release/domain-next.previous"
  rm -rf -- "$backup"
  if [[ -d "$PP_ROOT/.next" ]]; then
    cp -a "$PP_ROOT/.next" "$backup"
    printf 'save-next %s\n' "$backup" >> "$state"
  else
    printf 'remove-next\n' >> "$state"
  fi
}

pp_set_env_key() {
  local file="$1" key="$2" value="$3"
  PP_ENV_FILE="$file" PP_ENV_KEY="$key" PP_ENV_VALUE="$value" python3 - <<'PY'
import os
from pathlib import Path
path = Path(os.environ["PP_ENV_FILE"])
key = os.environ["PP_ENV_KEY"]
value = os.environ["PP_ENV_VALUE"]
info = path.stat()
lines = path.read_text(encoding="utf-8").splitlines()
found = False
out = []
for line in lines:
    if line.startswith(key + "="):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
os.chmod(path, info.st_mode & 0o777)
os.chown(path, info.st_uid, info.st_gid)
PY
}

pp_env_value() {
  local file="$1" key="$2"
  python3 - "$file" "$key" <<'PY'
import sys
from pathlib import Path
file_name, key = sys.argv[1:]
for line in Path(file_name).read_text(encoding="utf-8").splitlines():
    if line.startswith(key + "="):
        print(line.split("=", 1)[1])
        break
PY
}

pp_ufw_allows_web() {
  local text="$1"
  [[ "$text" == *"80/tcp"* && "$text" == *"443/tcp"* ]]
}

pp_domain_public_urls() {
  local domain="$1" origin="https://${1}"
  pp_set_env_key "$PP_ROOT/config/api.env" CORS_ORIGINS "$origin"
  pp_set_env_key "$PP_ROOT/config/web.env" NEXT_PUBLIC_API_BASE "$origin"
}

pp_domain_restore_urls() {
  local failed=0
  pp_set_env_key "$PP_ROOT/config/api.env" CORS_ORIGINS "$1" || failed=1
  pp_set_env_key "$PP_ROOT/config/web.env" NEXT_PUBLIC_API_BASE "$2" || failed=1
  return "$failed"
}

pp_domain_missing_packages() {
  local name
  for name in nginx certbot; do
    command -v "$name" >/dev/null 2>&1 || printf '%s\n' "$name"
  done
}

pp_domain_confirm() {
  local answer
  [[ -r /dev/tty ]] || return 1
  read -r -p "$1" answer </dev/tty
  [[ "$answer" == "y" ]]
}

pp_domain_apt() {
  apt-get "$@"
}

pp_domain_prepare_packages() {
  local missing=() name
  while IFS= read -r name; do
    [[ -n "$name" ]] && missing+=("$name")
  done < <(pp_domain_missing_packages)
  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi
  echo "Domain setup needs these Ubuntu packages: ${missing[*]}"
  echo "They are installed only from the Ubuntu repositories, and only for Domain & SSL."
  if ! pp_domain_confirm "Install them now? [y/N] "; then
    echo "Package installation was not approved. No domain configuration was changed." >&2
    return 1
  fi
  if ! pp_domain_apt update || ! pp_domain_apt install -y "${missing[@]}"; then
    echo "Package installation failed. No domain configuration was changed. Installed packages were not removed." >&2
    return 1
  fi
  pp_domain_missing_packages | grep -q . && return 1
  return 0
}

pp_domain_nginx_test() {
  nginx -t
}

pp_domain_nginx_reload() {
  if systemctl is-active --quiet nginx; then
    systemctl reload nginx || return 1
  else
    systemctl enable --now nginx || return 1
  fi
  systemctl is-active --quiet nginx
}

pp_domain_prepare_acme_webroot() {
  local challenge="$PP_ACME_ROOT/.well-known/acme-challenge" path metadata owner mode
  if [[ "$PP_ACME_ROOT" != /* || "$(realpath -m -- "$challenge")" != "$challenge" ]]; then
    echo 'The ACME webroot must be an absolute path without symlinks or traversal.' >&2
    return 1
  fi
  for path in "$PP_ACME_ROOT" "$PP_ACME_ROOT/.well-known" "$challenge"; do
    if [[ -e "$path" ]]; then
      if [[ ! -d "$path" ]] || ! metadata="$(stat -c '%u %a' -- "$path")" ||
        ! read -r owner mode <<<"$metadata" ||
        [[ "$owner" != "$EUID" || $((8#$mode & 022)) -ne 0 ]]; then
        echo 'An ACME webroot directory has unsafe ownership or write permissions.' >&2
        return 1
      fi
      [[ "$mode" == 755 ]] && continue
    fi
    install -d -m 755 -- "$path" || return 1
  done
}

pp_domain_acme_probe() (
  local domain="$1" challenge="$PP_ACME_ROOT/.well-known/acme-challenge"
  local probe response='' token attempt code failure status
  probe="$(mktemp -- "$challenge/pc-acme-probe.XXXXXXXXXXXX")" || {
    echo 'Could not create the ACME probe. Check the webroot permissions.' >&2
    return 1
  }
  trap 'status=$?; rm -f -- "$probe" "$response" || exit 1; exit "$status"' EXIT
  trap 'exit 1' HUP INT TERM
  response="$(mktemp)" || return 1
  token="${probe##*/}"
  if ! printf '%s' "$token" > "$probe" || ! chmod 644 -- "$probe"; then
    echo 'Could not make the ACME probe readable by Nginx.' >&2
    return 1
  fi

  for attempt in 1 2 3 4 5; do
    if code="$(curl --disable --noproxy '*' --silent --connect-timeout 1 --max-time 2 \
        --max-filesize 4096 --output "$response" --write-out '%{http_code}' \
        -H "Host: ${domain}" "http://127.0.0.1/.well-known/acme-challenge/${token}")"; then
      case "$code" in
        200)
          cmp -s -- "$probe" "$response" && return 0
          failure='HTTP 200 with an unexpected response body'
          ;;
        403) failure='HTTP 403 (check file permissions and directory traversal)' ;;
        404) failure='HTTP 404 (check the virtual host, webroot, and reload readiness)' ;;
        *) failure="HTTP ${code}" ;;
      esac
    else
      failure="curl exit $? (loopback request failed)"
    fi
    [[ "$attempt" == 5 ]] || sleep 1
  done
  printf 'ACME probe failed after %s attempts: %s.\n' "$attempt" "$failure" >&2
  return 1
)

pp_domain_cert_files_present() {
  [[ -f "$PP_CERT_FULLCHAIN" && -f "$PP_CERT_PRIVKEY" ]]
}

# OpenSSL 3.0 exits 0 from -checkhost even when the name does not match.
pp_domain_cert_name_ok() {
  local domain="$1" result
  result="$(openssl x509 -in "$PP_CERT_FULLCHAIN" -noout -checkhost "$domain" 2>/dev/null)" || return 1
  [[ "$result" == "Hostname $domain does match certificate" ]]
}

pp_domain_cert_time_ok() {
  local now start
  openssl x509 -in "$PP_CERT_FULLCHAIN" -noout -checkend 0 >/dev/null 2>&1 || return 1
  now="$(date -u +%s)"
  start="$(date -u -d "$(openssl x509 -in "$PP_CERT_FULLCHAIN" -noout -startdate | sed 's/^[^=]*=//')" +%s)" || return 1
  [[ "$now" -ge "$start" ]]
}

pp_domain_cert_key_ok() {
  local cert_pub key_pub
  cert_pub="$(openssl x509 -in "$PP_CERT_FULLCHAIN" -noout -pubkey 2>/dev/null | openssl sha256)"
  key_pub="$(openssl pkey -in "$PP_CERT_PRIVKEY" -pubout 2>/dev/null | openssl sha256)"
  [[ -n "$cert_pub" && "$cert_pub" == "$key_pub" ]]
}

pp_domain_cert_covers() {
  local domain="$1"
  pp_domain_cert_files_present || return 1
  command -v openssl >/dev/null 2>&1 || return 1
  pp_domain_cert_name_ok "$domain" || return 1
  pp_domain_cert_time_ok || return 1
  pp_domain_cert_key_ok
}

pp_domain_same_https() {
  local domain="$1"
  [[ -f "$PP_NGINX_SITE" ]] || return 1
  grep -q "server_name ${domain};" "$PP_NGINX_SITE" || return 1
  grep -q 'ssl_certificate ' "$PP_NGINX_SITE" || return 1
  pp_domain_cert_covers "$domain"
}

pp_domain_issue_cert() {
  local domain="$1"
  if pp_domain_cert_covers "$domain"; then
    if openssl x509 -in "$PP_CERT_FULLCHAIN" -noout -checkend 2592000 >/dev/null 2>&1; then
      echo "The existing certificate already covers ${domain}. A new certificate was not requested."
      return 0
    fi
    echo "The certificate covers ${domain} and is near expiry. Renewing the existing certificate."
    certbot renew --cert-name partnerconsole --non-interactive || return 1
    pp_domain_cert_covers "$domain"
    return
  fi
  certbot certonly --webroot -w "$PP_ACME_ROOT" -d "$domain" --cert-name partnerconsole \
    --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring
}

# Next.js reads NEXT_PUBLIC_API_BASE from the build environment, not from
# config/web.env, and embeds it in the browser JavaScript.
pp_domain_build_web() {
  local origin="$1"
  [[ -f "$PP_ROOT/package.json" ]] || return 0
  (
    unset NEXT_PUBLIC_API_BASE
    # shellcheck disable=SC1091
    source "$PP_ROOT/scripts/common.sh" || exit 1
    if [[ ! -f "$PP_ROOT/config/web.env" ]]; then
      echo "config/web.env is missing. The web app was not built." >&2
      exit 1
    fi
    if ! pp_load_env_file "$PP_ROOT/config/web.env"; then
      echo "config/web.env could not be read. The web app was not built." >&2
      exit 1
    fi
    if [[ -z "$origin" || "${NEXT_PUBLIC_API_BASE:-}" != "$origin" ]]; then
      echo "NEXT_PUBLIC_API_BASE in config/web.env is not ${origin:-the HTTPS origin}. The web app was not built." >&2
      exit 1
    fi
    cd "$PP_ROOT" && npm run build --prefix "$PP_ROOT"
  )
}

# Accept sign-in JavaScript that embeds the HTTPS origin and has neither a
# runtime lookup nor the loopback fallback for the API base.
pp_domain_client_origin_ok() {
  local origin="$1" file found=0
  shift
  for file in "$@"; do
    [[ -f "$file" ]] || return 1
    if grep -Fq -e 'NEXT_PUBLIC_API_BASE' -e '127.0.0.1:8741' -- "$file"; then
      return 1
    fi
    if grep -Eq -- "${origin//./\\.}[\"'\`/]" "$file"; then
      found=1
    fi
  done
  [[ "$found" == 1 ]]
}

# Check the JavaScript that the active build loads for the sign-in pages.
pp_domain_bundle_ok() {
  local origin="$1" py list
  local -a files=()
  list="$(mktemp)"
  for py in python3 python; do
    command -v "$py" >/dev/null 2>&1 || continue
    "$py" -c 'import sys' >/dev/null 2>&1 || continue
    if "$py" - "$PP_ROOT/.next" > "$list" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
try:
    pages = json.loads((root / "app-build-manifest.json").read_text(encoding="utf-8"))["pages"]
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(1)
names = [name for name in pages if name == "/page" or (name.startswith("/gate/") and name.endswith("/page"))]
if "/page" not in names:
    raise SystemExit(1)
for rel in sorted({rel for name in names for rel in pages[name] if rel.endswith(".js")}):
    path = (root / rel).resolve()
    if root not in path.parents:
        raise SystemExit(1)
    print(path)
PY
    then
      mapfile -t files < <(tr -d '\r' < "$list")
    fi
    break
  done
  rm -f "$list"
  [[ "${#files[@]}" -gt 0 ]] || return 1
  pp_domain_client_origin_ok "$origin" "${files[@]}"
}

pp_domain_json_ok() {
  local body="$1" py
  for py in python3 python; do
    command -v "$py" >/dev/null 2>&1 || continue
    "$py" -c 'import sys' >/dev/null 2>&1 || continue
    printf '%s' "$body" | "$py" -c 'import json,sys
try:
    data=json.loads(sys.stdin.read() or "")
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if isinstance(data, dict) and data.get("ok") is True else 1)' && return 0
  done
  return 1
}

pp_domain_redirect_ok() {
  local location="$1" domain="$2"
  location="${location%$'\r'}"
  [[ "$location" == "https://${domain}/login" || "$location" == "https://${domain}/login/" ]]
}

pp_domain_login_body_ok() {
  local body="$1"
  [[ "$body" == *"ورود"* ]] || return 1
  [[ "$body" != *"Internal Server Error"* && "$body" != *"Application error"* && "$body" != *"502 Bad Gateway"* && "$body" != *"503 Service"* ]]
}

pp_domain_fetch() {
  local domain="$1" path="$2" hdr body code location
  hdr="$(mktemp)"
  body="$(mktemp)"
  if ! code="$(curl --disable --noproxy '*' -sS --resolve "${domain}:443:127.0.0.1" \
      --connect-timeout 5 --max-time 20 \
      --retry 2 --retry-delay 1 --retry-connrefused \
      -D "$hdr" -o "$body" -w '%{http_code}' \
      "https://${domain}${path}")"; then
    rm -f "$hdr" "$body"
    return 1
  fi
  location="$(awk 'tolower($1)=="location:" {print $2}' "$hdr" | tr -d '\r' | tail -n 1)"
  printf '%s\n%s\n' "$code" "${location:--}"
  cat "$body"
  rm -f "$hdr" "$body"
}

pp_domain_cors_ok() {
  local domain="$1" response
  response="$(curl --disable --noproxy '*' -sS --resolve "${domain}:443:127.0.0.1" \
    --connect-timeout 5 --max-time 10 -D - -o /dev/null -w '\nstatus: %{http_code}\n' \
    -X OPTIONS -H "Origin: https://${domain}" -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: content-type,authorization' \
    "https://${domain}/api/admin/auth/login")" || return 1
  awk -v origin="https://$domain" '
    { sub(/\r$/, "") }
    tolower($1)=="access-control-allow-origin:" && $2==origin { allowed=1 }
    tolower($1)=="access-control-allow-methods:" && $0 ~ /(^|[ ,])POST([ ,]|$)/ { method=1 }
    tolower($1)=="access-control-allow-headers:" {
      headers=tolower($0)
      if (headers ~ /(^|[ ,])authorization([ ,]|$)/) auth=1
      if (headers ~ /(^|[ ,])content-type([ ,]|$)/) content=1
    }
    $1=="status:" && $2=="200" { success=1 }
    END { exit !(allowed && method && auth && content && success) }
  ' <<<"$response"
}

pp_domain_https_probe() {
  local domain="$1" try result code location body page_code page_body scripts path assets
  local -a served=()
  for try in 1 2 3 4 5; do
    if result="$(pp_domain_fetch "$domain" "/api/health")"; then
      break
    fi
    [[ "$try" -eq 5 ]] && { echo "HTTPS did not connect. The certificate or the service was not accepted." >&2; return 1; }
    sleep 2
  done
  code="$(printf '%s\n' "$result" | sed -n '1p')"
  location="$(printf '%s\n' "$result" | sed -n '2p')"
  body="$(printf '%s\n' "$result" | sed -n '3,$p')"
  case "$code" in
    404|500|502|503)
      echo "API health returned HTTP ${code}." >&2
      return 1
      ;;
    200) ;;
    *)
      echo "API health returned HTTP ${code}." >&2
      return 1
      ;;
  esac
  if ! pp_domain_json_ok "$body"; then
    echo "API health was not a JSON object with ok true." >&2
    return 1
  fi
  if ! result="$(pp_domain_fetch "$domain" "/")"; then
    echo "The login page did not connect." >&2
    return 1
  fi
  page_code="$(printf '%s\n' "$result" | sed -n '1p')"
  location="$(printf '%s\n' "$result" | sed -n '2p')"
  page_body="$(printf '%s\n' "$result" | sed -n '3,$p')"
  case "$page_code" in
    301|302|307|308)
      pp_domain_redirect_ok "$location" "$domain" || { echo "The login redirect ${location} is not an HTTPS redirect to /login on this domain." >&2; return 1; }
      if ! result="$(pp_domain_fetch "$domain" "/login")"; then
        echo "The login page did not connect." >&2
        return 1
      fi
      page_code="$(printf '%s\n' "$result" | sed -n '1p')"
      page_body="$(printf '%s\n' "$result" | sed -n '3,$p')"
      ;;
  esac
  case "$page_code" in
    404|500|502|503)
      echo "The login page returned HTTP ${page_code}." >&2
      return 1
      ;;
    200) ;;
    *)
      echo "The login page returned HTTP ${page_code}." >&2
      return 1
      ;;
  esac
  pp_domain_login_body_ok "$page_body" || { echo "The login page did not contain the expected sign-in content." >&2; return 1; }
  if ! scripts="$(grep -oE '/_next/static/[A-Za-z0-9._/-]+\.js' <<<"$page_body" | sort -u)" || [[ -z "$scripts" ]]; then
    echo "The HTTPS login page did not reference any JavaScript." >&2
    return 1
  fi
  assets="$(mktemp -d)"
  while IFS= read -r path; do
    if ! result="$(pp_domain_fetch "$domain" "$path")" || [[ "${result%%$'\n'*}" != 200 ]]; then
      echo "A JavaScript asset on the HTTPS login page did not return HTTP 200." >&2
      rm -rf -- "$assets"
      return 1
    fi
    served+=("$assets/${#served[@]}.js")
    printf '%s\n' "$result" | sed -n '3,$p' > "${served[-1]}"
  done <<<"$scripts"
  if ! pp_domain_client_origin_ok "https://${domain}" "${served[@]}"; then
    echo "The HTTPS login page JavaScript does not use https://${domain} as its API origin." >&2
    rm -rf -- "$assets"
    return 1
  fi
  rm -rf -- "$assets"
  pp_domain_cors_ok "$domain" || { echo "The API did not allow the HTTPS origin in its CORS preflight." >&2; return 1; }
}

pp_domain_install_renew_hook() {
  local dir mode
  dir="$(dirname "$PP_RENEW_HOOK")"
  install -d -o root -g root -m 755 "$dir"
  pp_domain_renew_hook_text > "$PP_RENEW_HOOK"
  chown root:root "$PP_RENEW_HOOK"
  chmod 755 "$PP_RENEW_HOOK"
  mode="$(stat -c '%a' "$PP_RENEW_HOOK")"
  [[ "$(stat -c '%u' "$PP_RENEW_HOOK")" == "0" && $((8#$mode & 022)) -eq 0 ]]
}

pp_domain_enable_timer() {
  if systemctl list-unit-files 'certbot.timer' --no-legend 2>/dev/null | grep -q '^certbot.timer'; then
    systemctl enable --now certbot.timer || echo "certbot.timer could not be enabled. Renewal will not run on a schedule until it is enabled."
  else
    echo "certbot.timer is not installed. Renewal was not scheduled."
  fi
  echo "After a successful renewal of the Partner Console certificate, the deploy hook runs nginx -t and reloads nginx only if that test passes."
  echo "If nginx -t fails, the hook does not reload nginx and certbot reports the hook error. An active timer does not by itself prove that renewal or reload succeeded."
}

pp_domain_fail_back() {
  local state="$1" cors="$2" api="$3" message="$4" restart_previous="${5:-0}" files_restored=1
  echo "$message" >&2
  if [[ -n "$cors" || -n "$api" ]]; then
    if ! pp_domain_restore_urls "$cors" "$api"; then
      files_restored=0
      echo "The previous public URLs could not be restored." >&2
    fi
  fi
  if ! pp_domain_rollback "$state"; then
    files_restored=0
    echo "The previous site or web build could not be fully restored." >&2
  fi
  if ! pp_domain_nginx_test; then
    echo "nginx -t failed after the rollback. Nginx was not reloaded." >&2
  elif [[ "$files_restored" == 1 ]] && systemctl is-active --quiet nginx; then
    systemctl reload nginx || echo "Nginx could not reload the restored configuration." >&2
  fi
  if declare -F apply_permissions >/dev/null 2>&1; then
    if ! apply_permissions; then
      files_restored=0
      echo "Permissions could not be restored after the domain change failed." >&2
    fi
  fi
  if [[ "$restart_previous" == 1 ]]; then
    if [[ "$files_restored" != 1 ]] || ! restart_app; then
      echo "The previous application runtime could not be verified after rollback." >&2
    fi
  fi
  return 1
}

pp_domain_activate() {
  local domain="$1" state previous_cors="" previous_api="" service_state=0 restarted=0
  if ! declare -F app_running >/dev/null || ! declare -F restart_app >/dev/null; then
    echo "Use the Partner Console launcher to check the application before configuring HTTPS." >&2
    return 1
  fi
  app_running || service_state=$?
  if [[ "$service_state" -gt 1 ]]; then
    echo "Cannot verify the application processes and ports. Domain setup stopped." >&2
    return 1
  fi
  state="${PP_DOMAIN_STATE:-$PP_ROOT/.release/domain-state}"
  install -d -m 700 "$PP_ROOT/.release"
  : > "$state"
  if pp_domain_same_https "$domain"; then
    if [[ "$service_state" == 0 ]]; then pp_domain_https_probe "$domain" || return 1; fi
    pp_domain_install_renew_hook || return 1
    pp_domain_enable_timer
    echo "This domain is already active. The certificate was not requested again and the current site was left in place."
    return 0
  fi
  pp_domain_remember_site "$state" || return 1
  pp_domain_prepare_acme_webroot || return 1
  install -d -m 755 "$(dirname "$PP_NGINX_SITE")" "$(dirname "$PP_NGINX_LINK")"
  # Replace an existing site symlink before writing through the site path.
  if [[ -L "$PP_NGINX_SITE" ]] && ! rm -f -- "$PP_NGINX_SITE"; then
    echo "The Partner Console site symlink could not be removed." >&2
    return 1
  fi
  if ! pp_nginx_http_config "$domain" > "$PP_NGINX_SITE" || ! ln -sfnT -- "$PP_NGINX_SITE" "$PP_NGINX_LINK"; then
    pp_domain_fail_back "$state" "" "" "The ACME site could not be written or enabled." || return 1
  fi
  if ! pp_domain_nginx_test; then
    pp_domain_fail_back "$state" "" "" "nginx -t failed for the ACME site." || return 1
  fi
  if ! pp_domain_nginx_reload; then
    pp_domain_fail_back "$state" "" "" "nginx could not be reloaded for the ACME site." || return 1
  fi
  if ! pp_domain_acme_probe "$domain"; then
    pp_domain_fail_back "$state" "" "" "The ACME challenge path was not reachable. The certificate was not requested." || return 1
  fi
  if ! pp_domain_issue_cert "$domain"; then
    pp_domain_fail_back "$state" "" "" "The certificate was not issued." || return 1
  fi
  if ! pp_domain_cert_files_present; then
    pp_domain_fail_back "$state" "" "" "The certificate files are not present. HTTPS was not enabled." || return 1
  fi
  previous_cors="$(pp_env_value "$PP_ROOT/config/api.env" CORS_ORIGINS)"
  previous_api="$(pp_env_value "$PP_ROOT/config/web.env" NEXT_PUBLIC_API_BASE)"
  pp_domain_save_next "$state"
  pp_domain_public_urls "$domain"
  if ! pp_domain_build_web "https://${domain}"; then
    pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "The web build failed." || return 1
  fi
  if ! pp_domain_bundle_ok "https://${domain}"; then
    pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "The sign-in JavaScript does not use https://${domain} as its API origin." || return 1
  fi
  if declare -F apply_permissions >/dev/null 2>&1; then
    if ! apply_permissions; then
      pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "Permissions could not be applied." || return 1
    fi
  fi
  if ! pp_nginx_https_config "$domain" > "$PP_NGINX_SITE"; then
    pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "The HTTPS site could not be written." || return 1
  fi
  if ! pp_domain_nginx_test; then
    pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "nginx -t failed for HTTPS." || return 1
  fi
  if ! pp_domain_nginx_reload; then
    pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "nginx reload failed for HTTPS." || return 1
  fi
  if [[ "$service_state" == 0 ]]; then
    echo "Restarting the application so it serves the new public origin."
    restarted=1
    if ! restart_app; then
      pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "The application restart failed." "$restarted" || return 1
    fi
    if ! pp_domain_https_probe "$domain"; then
      pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "HTTPS health failed after restart." "$restarted" || return 1
    fi
    echo "HTTPS was checked after restart: the certificate, API, login page JavaScript and CORS checks passed."
    echo "This check uses the local reverse proxy. It is not a browser login from the public internet."
  else
    if ! pp_domain_fetch "$domain" "/.well-known/acme-challenge/" >/dev/null; then
      pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "HTTPS did not connect through the local proxy." || return 1
    fi
    echo "The HTTPS certificate was checked. Application health checks are pending until the app is started."
  fi
  if ! pp_domain_install_renew_hook; then
    pp_domain_fail_back "$state" "$previous_cors" "$previous_api" "The renewal hook could not be installed." "$restarted" || return 1
  fi
  rm -rf -- "$PP_ROOT/.release/domain-next.previous" "$PP_ROOT/.release/domain-site.previous" "$PP_ROOT/.release/domain-link.previous"
  : > "$state"
  pp_domain_enable_timer
  echo "MySQL and the application ports stay on 127.0.0.1."
}

pp_domain_setup() {
  local domain answer ipv4 ipv6
  [[ -r /dev/tty ]] || { echo "Domain setup needs a terminal." >&2; return 1; }
  read -r -p "Domain name: " domain </dev/tty
  domain="${domain,,}"
  pp_domain_valid "$domain" || { echo "That domain name is not accepted." >&2; return 1; }
  ipv4="$(pp_domain_server_ipv4)"
  [[ -n "$ipv4" ]] || { echo "This server has no IPv4 address to publish." >&2; return 1; }
  ipv6="$(pp_domain_server_ipv6 || true)"
  pp_domain_ensure_dig || return 1
  if ! pp_domain_lookup "$domain" "$ipv4" "$ipv6"; then
    echo "DNS for ${domain} does not point only at this server, or the DNS lookup did not succeed." >&2
    echo "An IPv4-mapped address is not an AAAA record. A timeout or server failure is not an empty answer." >&2
    return 1
  fi
  if pp_foreign_web_listener "$(ss -lntup 2>/dev/null || true)"; then
    echo "Port 80 or 443 is used by something other than nginx." >&2
    return 1
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q 'Status: active'; then
    if ! pp_ufw_allows_web "$(ufw status)"; then
      echo "The system firewall is active and does not show 80/tcp and 443/tcp."
      echo "The VPS provider firewall must allow those ports as well. This command cannot see that firewall."
      if ! pp_domain_confirm "Allow only 80/tcp and 443/tcp in ufw? [y/N] "; then
        echo "The firewall was not changed." >&2
        return 1
      fi
      ufw allow 80/tcp
      ufw allow 443/tcp
    fi
  else
    echo "No active system firewall was found. Confirm that the VPS provider allows 80/tcp and 443/tcp."
  fi
  pp_domain_prepare_packages || return 1
  if command -v nginx >/dev/null 2>&1 && ! pp_domain_nginx_test; then
    echo "nginx -t failed before any Partner Console site change." >&2
    return 1
  fi
  pp_domain_activate "$domain" || return 1
  read -r -p "Start Partner Console automatically after reboot? [y/N] " answer </dev/tty
  if [[ "$answer" == "y" ]]; then
    pp_systemd_unit_text > "$PP_SYSTEMD_UNIT"
    systemctl daemon-reload
    systemctl enable partnerconsole.service
    echo "Boot start was enabled. It runs the existing start command once at boot."
    echo "It does not watch the processes and does not restart them after a crash."
    echo "Use partnerconsole status for the real API and Next.js state. The app still runs as ppv1."
  else
    echo "Boot start was not enabled. Existing installs stay manual until you choose this."
  fi
}

if [[ "${PP_DOMAIN_LIBRARY:-}" != "1" && "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  pp_domain_setup
fi
