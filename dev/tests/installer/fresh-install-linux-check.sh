#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$EUID" == 0 && "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]]
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
tools="$(cd "$(dirname "$0")" && pwd)"
assets="$(realpath "$1")"
expected="$2"
tag="$(sed -n 's/^RELEASE_TAG="\(.*\)"$/\1/p' "$assets/partnerconsole.sh")"
[[ ! -e /srv/partnerconsole && ! -e /usr/local/bin/partnerconsole ]]
# shellcheck source=/dev/null
source "$tools/release-http-fixture.sh"
setup_release_feed
cp -r "$assets" "$feed/$tag"
publish_fixture "$tag"
work="$(mktemp -d)"
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$work/password"
mkdir "$work/empty"
cat > "$feed/bin/git" <<'EOF'
#!/bin/bash
echo 'Git was invoked by the packaged installer' >&2
exit 97
EOF
chmod 700 "$feed/bin/git"
cd "$work/empty"
python3 "$tools/release-tty.py" "$work/password" bash "$tools/user-install.sh"
app=/srv/partnerconsole
grep -Fx "commit=$expected" "$app/.release/current"
grep -Fx "tag=$tag" "$app/.release/current"
cmp "$assets/partnerconsole.sh" "$app/partnerconsole.sh"
for excluded in dev tests .github .git; do [[ ! -e "$app/$excluded" ]]; done
curl -fsS http://127.0.0.1:3741/ | grep -F '<title>Partner Console</title>' >/dev/null
curl -fsS http://127.0.0.1:8741/openapi.json | python3 -c \
  'import sys,json; assert json.load(sys.stdin)["info"]["version"]==sys.argv[1]' "${tag#v}"
[[ "$(curl -fsS http://127.0.0.1:8741/api/health)" == '{"ok":true}' ]]
grep -Fxq "https://github.com/MoonlessWanderer/PartnerConsole/releases/download/$tag/partnerconsole-$tag.tar.gz" "$feed/requests"
echo "README fresh install, API, sign-in and runtime-only tree passed for $expected"
