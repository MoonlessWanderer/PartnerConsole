#!/usr/bin/env bash
# Builds the real Next.js app in a temporary custom install root and checks the
# API origin that the built sign-in pages use in a headless Chromium browser.
# Requires Node.js, npm, curl and Chrome, Chromium or Edge (set CHROME to override).
# Pass another domain.sh, for example a previous release, to test it instead.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
candidate="${1:-$repo/scripts/domain.sh}"
origin="https://console.example.test"
work="$(mktemp -d)"
root="$work/srv/pc-custom"
server=""

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok %s\n' "$*"; }

stop_server() {
  if [[ -n "$server" ]]; then
    kill "$server" 2>/dev/null || true
    wait "$server" 2>/dev/null || true
    server=""
  fi
}
cleanup() {
  stop_server
  rm -rf -- "$work"
}
trap cleanup EXIT

find_browser() {
  local name
  if [[ -n "${CHROME:-}" ]]; then printf '%s\n' "$CHROME"; return; fi
  for name in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge; do
    if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return; fi
  done
  for name in "/c/Program Files/Google/Chrome/Application/chrome.exe" \
    "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"; do
    if [[ -x "$name" ]]; then printf '%s\n' "$name"; return; fi
  done
  return 1
}

browser="$(find_browser)" || fail 'Chrome, Chromium or Edge is required'
command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || fail 'Node.js and npm are required'
port=$((37000 + RANDOM % 2000))

mkdir -p "$root"
(
  cd "$repo"
  git ls-files -z --cached --others --exclude-standard |
    while IFS= read -r -d '' path; do
      if [[ -e "$path" || -L "$path" ]]; then printf '%s\0' "$path"; fi
    done | tar --null -T - -cf -
) | tar -xf - -C "$root"
cp -- "$candidate" "$root/scripts/domain.sh"
npm ci --prefix "$root" --no-audit --no-fund >"$work/npm-ci.log" 2>&1 || fail "npm ci failed: $(tail -n 5 "$work/npm-ci.log")"
mkdir -p "$root/config"

start_server() {
  local _
  (cd "$root" && exec node node_modules/next/dist/bin/next start -H 127.0.0.1 -p "$port") >"$work/next.log" 2>&1 &
  server=$!
  for _ in $(seq 1 60); do
    if [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" || true)" == 200 ]]; then return 0; fi
    sleep 1
  done
  fail "next start did not serve the sign-in page: $(tail -n 5 "$work/next.log")"
}

# Prints the URL of the sign-in request sent by the page at PATH.
login_url() {
  local result
  result="$(node "$repo/dev/tests/installer/login-origin-probe.cjs" "$browser" "http://127.0.0.1:$port$1")" ||
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
  start_server
  admin="$(login_url /gate/k3p8n2)"
  partner="$(login_url /)"
  stop_server
  [[ "$admin" == "$want/api/admin/auth/login" ]] || fail "$label: administrator sign-in used $admin, expected $want/api/admin/auth/login"
  [[ "$partner" == "$want/api/partner/auth/login" ]] || fail "$label: partner sign-in used $partner, expected $want/api/partner/auth/login"
  ok "$label: browser sign-in requests use $want"
}

# A fresh install without a domain keeps the loopback API base. Domain & SSL
# then writes the HTTPS origin before it builds the web app.
domain_build() {
  local inherited="$1"
  printf 'NEXT_PUBLIC_API_BASE=http://127.0.0.1:8741\n' > "$root/config/web.env"
  printf 'NEXT_PUBLIC_API_BASE=%s\n' "$origin" > "$root/config/web.env"
  (
    unset NEXT_PUBLIC_API_BASE
    if [[ -n "$inherited" ]]; then export NEXT_PUBLIC_API_BASE="$inherited"; fi
    PP_ROOT="$root"
    PP_DOMAIN_LIBRARY=1
    # shellcheck disable=SC1090
    source "$root/scripts/domain.sh"
    pp_domain_build_web "$origin" >"$work/domain-build.log" 2>&1 || fail "the Domain & SSL build failed: $(tail -n 5 "$work/domain-build.log")"
  ) || exit 1
}
domain_build ''
ok 'Domain & SSL build ran the real Next.js build in a custom install root'
expect_login_origin "$origin" 'Domain & SSL build'
domain_build https://inherited.example.test
expect_login_origin "$origin" 'Domain & SSL build with a stale inherited NEXT_PUBLIC_API_BASE'

(
  PP_ROOT="$root"
  PP_DOMAIN_LIBRARY=1
  # shellcheck disable=SC1090
  source "$root/scripts/domain.sh"
  declare -F pp_domain_bundle_ok >/dev/null || fail 'this domain.sh has no client bundle check'
  pp_domain_bundle_ok "$origin" || fail 'the bundle check rejected the correct HTTPS origin'
  printf 'NEXT_PUBLIC_API_BASE=https://changed.example.test\n' > "$root/config/web.env"
  if pp_domain_bundle_ok https://changed.example.test; then fail 'an env change without a rebuild was accepted'; fi
) || exit 1
expect_login_origin "$origin" 'env changed without rebuild'
ok 'the bundle check accepted the built origin and rejected an env change without a rebuild'

# Previous releases built without loading web.env. The checks must reject it.
printf 'NEXT_PUBLIC_API_BASE=%s\n' "$origin" > "$root/config/web.env"
(
  unset NEXT_PUBLIC_API_BASE
  npm run build --prefix "$root" >"$work/plain-build.log" 2>&1 || fail "the plain build failed: $(tail -n 5 "$work/plain-build.log")"
  PP_ROOT="$root"
  PP_DOMAIN_LIBRARY=1
  # shellcheck disable=SC1090
  source "$root/scripts/domain.sh"
  if pp_domain_bundle_ok "$origin"; then fail 'the bundle check accepted a build without NEXT_PUBLIC_API_BASE'; fi
) || exit 1
expect_login_origin http://127.0.0.1:8741 'build without loading web.env'
ok 'the bundle check rejected the loopback fallback of a build that did not load web.env'

grep -Fq 'pp_load_env_file "$PP_ROOT/config/web.env"' "$root/install.sh" || fail 'install.sh no longer loads web.env'
[[ "$(grep -n -e 'pp_load_env_file "$PP_ROOT/config/web.env"' -e 'npm run build' "$root/install.sh" | cut -d: -f1 | tr '\n' ' ')" =~ ^([0-9]+)\ ([0-9]+)\ $ ]] &&
  (( BASH_REMATCH[1] < BASH_REMATCH[2] )) || fail 'install.sh does not load web.env before npm run build'
printf 'NEXT_PUBLIC_API_BASE=https://install.example.test\n' > "$root/config/web.env"
(
  unset NEXT_PUBLIC_API_BASE
  cd "$root"
  PP_ROOT="$root"
  # shellcheck disable=SC1091
  source "$root/scripts/common.sh"
  pp_load_env_file "$PP_ROOT/config/web.env"
  npm run build --prefix "$PP_ROOT" >"$work/install-build.log" 2>&1 || fail "the install.sh build sequence failed: $(tail -n 5 "$work/install-build.log")"
) || exit 1
expect_login_origin https://install.example.test 'install.sh build sequence'
printf 'next build checks passed\n'
