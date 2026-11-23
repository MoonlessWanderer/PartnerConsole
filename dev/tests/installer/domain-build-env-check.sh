#!/usr/bin/env bash
# Checks how Domain & SSL prepares the web build environment and validates the
# sign-in JavaScript. npm is replaced; domain-build-next-check.sh runs the real build.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
origin="https://console.example.test"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok %s\n' "$*"; }

PP_DOMAIN_LIBRARY=1
# shellcheck disable=SC1091
source "$repo/scripts/domain.sh"
PP_ROOT="$work/srv/custom-root"
mkdir -p "$PP_ROOT/scripts" "$PP_ROOT/config"
cp "$repo/scripts/common.sh" "$PP_ROOT/scripts/common.sh"
printf '{}\n' > "$PP_ROOT/package.json"
printf 'SECRET_KEY=api-secret-sentinel\n' > "$PP_ROOT/config/api.env"
calls="$work/npm-calls"
npm_status=0

# shellcheck disable=SC2329 # Called by pp_domain_build_web.
npm() {
  printf '%s|%s|%s|%s\n' "${NEXT_PUBLIC_API_BASE-unset}" "$PWD" "$(umask)" "$*" >> "$calls"
  return "$npm_status"
}

build_case() {
  local web="$1" inherited="$2" expected="${3-$origin}" status=0
  rm -f "$calls" "$PP_ROOT/config/web.env"
  if [[ "$web" != MISSING ]]; then printf '%b' "$web" > "$PP_ROOT/config/web.env"; fi
  set +e
  if [[ "$inherited" == unset ]]; then
    (unset NEXT_PUBLIC_API_BASE; pp_domain_build_web "$expected") > "$work/out" 2>&1
  else
    (export NEXT_PUBLIC_API_BASE="$inherited"; pp_domain_build_web "$expected") > "$work/out" 2>&1
  fi
  status=$?
  set -e
  ! grep -Fq -e api-secret-sentinel -e web-secret-sentinel "$work/out" || fail 'the build printed a configuration value'
  return "$status"
}

expect_rejected() {
  local label="$1"
  shift
  if build_case "$@"; then fail "$label was accepted"; fi
  [[ ! -e "$calls" ]] || fail "$label still ran npm"
  ok "$label stops before npm"
}

umask 027
build_case "NEXT_PUBLIC_API_BASE=$origin\nWEB_TOKEN=web-secret-sentinel\n" unset || fail "a valid web.env was rejected: $(cat "$work/out")"
IFS='|' read -r seen cwd mask args < "$calls"
[[ "$seen" == "$origin" ]] || fail "npm received $seen"
[[ "$cwd" -ef "$PP_ROOT" ]] || fail "npm ran in $cwd"
[[ "$mask" == 0027 ]] || fail "the build changed the umask to $mask"
[[ "$args" == "run build --prefix $PP_ROOT" ]] || fail "unexpected npm arguments $args"
[[ "$(umask)" == 0027 && ! -v NEXT_PUBLIC_API_BASE ]] || fail 'the build changed the caller environment'
ok 'web.env reaches npm in a custom install root without changing the caller'

build_case "NEXT_PUBLIC_API_BASE=$origin\n" https://inherited.example.test || fail 'an inherited value blocked a valid web.env'
[[ "$(cut -d'|' -f1 "$calls")" == "$origin" ]] || fail 'an inherited NEXT_PUBLIC_API_BASE overrode web.env'
ok 'web.env overrides an inherited NEXT_PUBLIC_API_BASE'

expect_rejected 'a web.env without the key and an inherited origin' "OTHER=1\n" "$origin"
expect_rejected 'a missing web.env' MISSING unset
expect_rejected 'an empty NEXT_PUBLIC_API_BASE' "NEXT_PUBLIC_API_BASE=\n" unset
expect_rejected 'a placeholder NEXT_PUBLIC_API_BASE' "NEXT_PUBLIC_API_BASE=https://panel.example.com\n" unset
expect_rejected 'a REPLACE_WITH placeholder' "NEXT_PUBLIC_API_BASE=https://REPLACE_WITH_DOMAIN\n" unset
expect_rejected 'the loopback API base' "NEXT_PUBLIC_API_BASE=http://127.0.0.1:8741\n" unset
expect_rejected 'another HTTPS origin' "NEXT_PUBLIC_API_BASE=https://other.example.test\n" unset
expect_rejected 'an HTTP origin for the domain' "NEXT_PUBLIC_API_BASE=http://console.example.test\n" unset
expect_rejected 'an origin with a path' "NEXT_PUBLIC_API_BASE=$origin/api\n" unset
expect_rejected 'a web.env line that is not KEY=VALUE' "NEXT_PUBLIC_API_BASE=$origin\nnot a setting\n" unset
expect_rejected 'a quoted value' "NEXT_PUBLIC_API_BASE=\"$origin\"\n" unset
expect_rejected 'a missing expected origin' "NEXT_PUBLIC_API_BASE=$origin\n" unset ''

printf 'NEXT_PUBLIC_API_BASE=%s\n' "$origin" > "$PP_ROOT/config/web.env"
chmod 640 "$PP_ROOT/config/web.env"
before="$(stat -c '%a %u %g %Y %s' "$PP_ROOT/config/web.env")"
npm_status=7
if build_case "NEXT_PUBLIC_API_BASE=$origin\n" unset; then fail 'a failed npm build was reported as success'; fi
[[ -s "$calls" ]] || fail 'npm was not run'
npm_status=0
printf 'NEXT_PUBLIC_API_BASE=%s\n' "$origin" > "$PP_ROOT/config/web.env"
chmod 640 "$PP_ROOT/config/web.env"
touch -d "@$(cut -d' ' -f4 <<<"$before")" "$PP_ROOT/config/web.env"
pp_domain_build_web "$origin" >/dev/null 2>&1 || fail 'the build failed'
[[ "$(stat -c '%a %u %g %Y %s' "$PP_ROOT/config/web.env")" == "$before" ]] || fail 'the build changed web.env'
ok 'a failed npm build fails and a build leaves web.env unchanged'

js="$work/js"
mkdir -p "$js"
printf 'let a="%s";fetch(`${a}/api${p}`)\n' "$origin" > "$js/good.js"
printf 'fetch(`%s/api/admin/auth/login`)\n' "$origin" > "$js/template.js"
printf 'let a=r(5704).env.NEXT_PUBLIC_API_BASE||"http://127.0.0.1:8741";\n' > "$js/runtime.js"
printf 'let a="http://127.0.0.1:8741";\n' > "$js/loopback.js"
printf 'let a="%s.evil.test";\n' "$origin" > "$js/lookalike.js"
printf 'let a="https://consoleXexample.test";\n' > "$js/dotless.js"
printf 'react-dom\n' > "$js/framework.js"
pp_domain_client_origin_ok "$origin" "$js/framework.js" "$js/good.js" || fail 'a bundle with the origin was rejected'
pp_domain_client_origin_ok "$origin" "$js/template.js" || fail 'an origin inside a template literal was rejected'
if pp_domain_client_origin_ok "$origin" "$js/good.js" "$js/runtime.js"; then fail 'a runtime API base lookup was accepted'; fi
if pp_domain_client_origin_ok "$origin" "$js/good.js" "$js/loopback.js"; then fail 'the loopback API base was accepted'; fi
if pp_domain_client_origin_ok "$origin" "$js/lookalike.js"; then fail 'a lookalike host was accepted'; fi
if pp_domain_client_origin_ok "$origin" "$js/dotless.js"; then fail 'a dot in the origin matched any character'; fi
if pp_domain_client_origin_ok "$origin" "$js/framework.js"; then fail 'a bundle without the origin was accepted'; fi
if pp_domain_client_origin_ok "$origin"; then fail 'no JavaScript was accepted'; fi
if pp_domain_client_origin_ok "$origin" "$js/absent.js"; then fail 'a missing file was accepted'; fi
ok 'client origin check'

next="$PP_ROOT/.next"
mkdir -p "$next/static/chunks/app/gate/k3p8n2" "$next/static/chunks/app/admin"
cp "$js/framework.js" "$next/static/chunks/main.js"
cp "$js/good.js" "$next/static/chunks/app/page.js"
cp "$js/good.js" "$next/static/chunks/app/gate/k3p8n2/page.js"
cp "$js/loopback.js" "$next/static/chunks/app/admin/page.js"
manifest() {
  printf '{"pages":{"/page":["static/chunks/main.js","static/chunks/app/page.js","static/css/a.css"],"/gate/k3p8n2/page":["static/chunks/main.js","%s"],"/admin/page":["static/chunks/app/admin/page.js"]}}\n' \
    "$1" > "$next/app-build-manifest.json"
}
manifest static/chunks/app/gate/k3p8n2/page.js
pp_domain_bundle_ok "$origin" || fail 'the active sign-in build was rejected'
if pp_domain_bundle_ok https://changed.example.test; then fail 'a build for another origin was accepted'; fi
manifest static/chunks/app/admin/page.js
if pp_domain_bundle_ok "$origin"; then fail 'a loopback administrator sign-in chunk was accepted'; fi
manifest ../../outside.js
if pp_domain_bundle_ok "$origin"; then fail 'a manifest entry outside .next was accepted'; fi
printf '{"pages":{"/gate/k3p8n2/page":["static/chunks/app/page.js"]}}\n' > "$next/app-build-manifest.json"
if pp_domain_bundle_ok "$origin"; then fail 'a manifest without the partner sign-in page was accepted'; fi
rm -f "$next/app-build-manifest.json"
if pp_domain_bundle_ok "$origin"; then fail 'a build without a manifest was accepted'; fi
ok 'active build bundle check'
printf 'domain build environment checks passed\n'
