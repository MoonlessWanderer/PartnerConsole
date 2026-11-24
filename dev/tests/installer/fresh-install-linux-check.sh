#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$EUID" == 0 && "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]]
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
tools="$(cd "$(dirname "$0")" && pwd)"
assets="$(realpath "$1")"
expected="$2"
mode="${3:-online}"
[[ "$mode" == online || "$mode" == archive ]]
tag="$(sed -n 's/^RELEASE_TAG="\(.*\)"$/\1/p' "$assets/partnerconsole.sh")"
[[ ! -e /srv/partnerconsole && ! -e /usr/local/bin/partnerconsole ]]
# shellcheck source=/dev/null
source "$tools/release-http-fixture.sh"
setup_release_feed
cp -r "$assets" "$PC_RELEASE_FIXTURE_DIR/$tag"
publish_fixture "$tag"
work="$(mktemp -d)"
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$work/password"
mkdir "$work/empty"
cat > "$PC_RELEASE_FIXTURE_DIR/bin/git" <<'EOF'
#!/bin/bash
printf '%s\n' git >> /srv/pc-release-fixture/blocked-requests
echo 'Git was invoked by the packaged installer' >&2
exit 97
EOF
chmod 700 "$PC_RELEASE_FIXTURE_DIR/bin/git"
cd "$work/empty"
if [[ "$mode" == archive ]]; then
  block_release_feed
  cp "$assets/partnerconsole.sh" ./partnerconsole.sh
  cp "$assets/partnerconsole-$tag.tar.gz" './selected archive.tar.gz'
  printf 'corrupt archive\n' > ./corrupt.tar.gz
  # The test already runs as root on this disposable runner.
  # shellcheck disable=SC2024
  if sudo bash ./partnerconsole.sh install --from-archive ./missing.tar.gz > "$work/missing.log" 2>&1; then exit 1; fi
  grep -F 'Local archive is not a readable regular file:' "$work/missing.log"
  # shellcheck disable=SC2024
  if sudo bash ./partnerconsole.sh install --from-archive ./corrupt.tar.gz > "$work/corrupt.log" 2>&1; then exit 1; fi
  grep -F "Local archive checksum mismatch for $tag:" "$work/corrupt.log"
  for path in .release/current config/api.env config/web.env; do [[ ! -e "/srv/partnerconsole/$path" ]]; done
  python3 "$tools/release-tty.py" "$work/password" sudo bash ./partnerconsole.sh install \
    --from-archive './selected archive.tar.gz' | tee "$work/install.log"
  sudo partnerconsole start
else
  python3 "$tools/release-tty.py" "$work/password" bash "$tools/user-install.sh" | tee "$work/install.log"
fi
app=/srv/partnerconsole
grep -Fx "commit=$expected" "$app/.release/current"
grep -Fx "tag=$tag" "$app/.release/current"
read -r archive_sha _ < <(sha256sum "$assets/partnerconsole-$tag.tar.gz")
grep -Fx "archive=$archive_sha" "$app/.release/current"
cmp "$assets/partnerconsole.sh" "$app/partnerconsole.sh"
grep -Fq "Installing Python dependencies with $app/deploy/requirements.lock.txt." "$work/install.log"
for unit in api web; do
  grep -Fxq "WorkingDirectory=$app" "$app/deploy/generated/partnerconsole-$unit.service"
  grep -Fxq 'User=ppv1' "$app/deploy/generated/partnerconsole-$unit.service"
done
grep -Fq 'proxy_pass http://127.0.0.1:8741;' "$app/deploy/generated/partnerconsole.conf"
! grep -REq '@ROOT@|@SERVICE_USER@' "$app/deploy/generated"
[[ ! -e "$app/release" ]]
echo 'Python dependencies installed with the deploy lockfile and Nginx/systemd templates rendered in deploy/generated.'
for excluded in dev tests .github .git; do [[ ! -e "$app/$excluded" ]]; done
curl -fsS http://127.0.0.1:3741/ | grep -F '<title>Partner Console</title>' >/dev/null
curl -fsS http://127.0.0.1:8741/openapi.json | python3 -c \
  'import sys,json; assert json.load(sys.stdin)["info"]["version"]==sys.argv[1]' "${tag#v}"
[[ "$(curl -fsS http://127.0.0.1:8741/api/health)" == '{"ok":true}' ]]
if [[ "$mode" == online ]]; then
  grep -Fxq "https://github.com/MoonlessWanderer/PartnerConsole/releases/download/$tag/partnerconsole-$tag.tar.gz" "$PC_RELEASE_FIXTURE_DIR/requests"
else
  [[ ! -s "$PC_RELEASE_FIXTURE_DIR/requests" ]]
  archive_test_had_no_requests
fi
echo "Fresh $mode install, API, sign-in and runtime-only tree passed for $expected"
