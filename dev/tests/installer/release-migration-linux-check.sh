#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$EUID" == 0 && "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]]
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
tools="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$tools/../../.." && pwd)"
a="$(realpath "$1")"
b="$(realpath "$2")"
tag_a="$(sed -n 's/^RELEASE_TAG="\(.*\)"$/\1/p' "$a/partnerconsole.sh")"
tag_b="$(sed -n 's/^RELEASE_TAG="\(.*\)"$/\1/p' "$b/partnerconsole.sh")"
dpkg --compare-versions "${tag_a#v}" gt 1.0.9
dpkg --compare-versions "${tag_b#v}" gt "${tag_a#v}"
expected="$(git -c safe.directory="$repo" -C "$repo" rev-parse HEAD)"
app=/srv/partnerconsole
: "${BROWSER_USER:?use a non-root browser account}"
: "${PLAYWRIGHT_MODULE:?use the pinned browser driver}"
[[ "$(id -u "$BROWSER_USER")" != 0 ]]
browser="$(command -v google-chrome || command -v chromium)"
browser_home="$(getent passwd "$BROWSER_USER" | cut -d: -f6)"
[[ ! -e "$app" && ! -e /usr/local/bin/partnerconsole ]]
work="$(mktemp -d)"
chmod 755 "$work"
cleanup() {
  local status=$? log
  if [[ "$status" != 0 ]]; then
    for log in "$work/browser.log" "$work/simulator.log" "$app/logs/api.log" "$app/logs/web.log"; do
      if [[ -f "$log" ]]; then tail -n 25 "$log" >&2; fi
    done
  fi
  if [[ -n "${probe_pid:-}" ]]; then kill "$probe_pid" 2>/dev/null || true; fi
  if [[ -n "${lab_pid:-}" ]]; then kill "$lab_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$work/password"
mkdir "$work/public"
(
  cd "$work/public"
  url=https://github.com/MoonlessWanderer/PartnerConsole/releases/download/v1.0.9
  for asset in partnerconsole.sh partnerconsole-v1.0.9.tar.gz SHA256SUMS; do
    /usr/bin/curl --fail --silent --show-error --location --remote-name "$url/$asset"
  done
  sha256sum --check --strict <<'SUMS'
60da7f7e5728c08c5aefd337612ebe9cef5dea118da30932c0eb7f1875bfbd91  partnerconsole.sh
f8951b01590e99747ab4cd6b6b2f98dfa6377c4a37ce691b3a8d05bcb2fb081a  partnerconsole-v1.0.9.tar.gz
c3c7f54fbdb3d8b2a9def56e7299f3def8d8fe1950ad535b2b1cb5224d07d961  SHA256SUMS
SUMS
  sha256sum --check --strict SHA256SUMS
  # Keep the public path active until the baseline installation is complete.
  python3 "$tools/release-tty.py" "$work/password" bash ./partnerconsole.sh install
)
grep -Fx 'tag=v1.0.9' "$app/.release/current"
grep -Fx 'commit=a5e82fdd960a5cc7d933240c309f2e34b5356673' "$app/.release/current"
grep -Fx 'archive=f8951b01590e99747ab4cd6b6b2f98dfa6377c4a37ce691b3a8d05bcb2fb081a' "$app/.release/current"
cmp "$work/public/partnerconsole.sh" "$app/partnerconsole.sh"
version_is() {
  curl -fsS http://127.0.0.1:8741/openapi.json | python3 -c \
    'import json,sys; assert json.load(sys.stdin)["info"]["version"] == sys.argv[1]; print("Running API version:", sys.argv[1])' "$1"
}
# shellcheck source=/dev/null
source "$tools/release-http-fixture.sh"
setup_release_feed
cp -r "$a" "$PC_RELEASE_FIXTURE_DIR/$tag_a"
cp -r "$b" "$PC_RELEASE_FIXTURE_DIR/$tag_b"
cat > "$PC_RELEASE_FIXTURE_DIR/bin/git" <<'EOF'
#!/bin/bash
printf '%s\n' git >> /srv/pc-release-fixture/blocked-requests
echo 'Git was invoked after the legacy installation' >&2
exit 97
EOF
chmod 700 "$PC_RELEASE_FIXTURE_DIR/bin/git"
grep -Fxq 'PANEL_ENCRYPTION_KEY=' "$app/config/api.env"
panel_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
sed -i -e 's|^PANEL_PRIVATE_ORIGINS=$|PANEL_PRIVATE_ORIGINS=http://127.0.0.1:18788|' \
  -e "s|^PANEL_ENCRYPTION_KEY=$|PANEL_ENCRYPTION_KEY=$panel_key|" "$app/config/api.env"
unset panel_key
chown --reference="$app/config/web.env" "$app/config/api.env"
chmod --reference="$app/config/web.env" "$app/config/api.env"
sysconfig="$work/config"
mkdir "$sysconfig"
cp "$app/config/api.env" "$app/config/web.env" "$sysconfig/"
/usr/local/bin/partnerconsole start
PYTHONPATH="$repo/dev:$repo/dev/tests/backend" "$app/backend/.venv/bin/python" \
  -m uvicorn financial_panel_lab:app --host 127.0.0.1 --port 18788 > "$work/simulator.log" 2>&1 &
lab_pid=$!
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18788/_lab/health >/dev/null 2>&1; then break; fi
  sleep 1
done
sample_state() { "$app/backend/.venv/bin/python" "$tools/release-migration-data.py" "$1" "$work/password"; }
version_is 1.0.9
sample_state seed > "$work/before.json"
install -d -o "$BROWSER_USER" -m 700 "$work/browser"
runuser -u "$BROWSER_USER" -- env PATH="$PATH" HOME="$browser_home" PLAYWRIGHT_MODULE="$PLAYWRIGHT_MODULE" \
  PC_FIXTURE_ADMIN=admin PC_FIXTURE_PASSWORD="$(< "$work/password")" \
  node "$tools/financial-upgrade-browser.cjs" "$browser" "$work/browser" >"$work/browser.log" 2>&1 &
probe_pid=$!
wait_for_browser() {
  local attempt
  for ((attempt=0; attempt<180; attempt++)); do
    [[ -f "$work/browser/$1" ]] && return
    kill -0 "$probe_pid" 2>/dev/null || break
    sleep 1
  done
  echo "Browser did not finish $1" >&2
  return 1
}
wait_for_browser ready
account_ids() {
  mysql --protocol=socket -N -e 'SELECT id,username FROM partnerconsole.admin_users ORDER BY id;
                                SELECT id,username FROM partnerconsole.partners ORDER BY id;'
}
account_ids > "$work/accounts-before"

preserved() {
  cmp "$sysconfig/api.env" "$app/config/api.env"
  cmp "$sysconfig/web.env" "$app/config/web.env"
  sample_state check > "$work/after.json"
  cmp "$work/before.json" "$work/after.json"
  account_ids | cmp "$work/accounts-before" -
}
runtime() {
  local tag="$1" assets="$2" archive_sha
  grep -Fxq "tag=$tag" "$app/.release/current"
  grep -Fxq "commit=$expected" "$app/.release/current"
  read -r archive_sha _ < <(sha256sum "$assets/partnerconsole-$tag.tar.gz")
  grep -Fx "archive=$archive_sha" "$app/.release/current"
  cmp "$assets/partnerconsole.sh" "$app/partnerconsole.sh"
  version_is "${tag#v}"
}
publish_fixture "$tag_a"
block_release_feed
update_from_a=(sudo bash "$a/partnerconsole.sh" update --from-archive "$a/partnerconsole-$tag_a.tar.gz")
cp "$app/.release/current" "$work/public-marker"
# Use the real, differently versioned candidate B archive with candidate A's installer.
# Already root on this disposable runner, including the log redirect.
# shellcheck disable=SC2024
if sudo bash "$a/partnerconsole.sh" update --from-archive "$b/partnerconsole-$tag_b.tar.gz" > "$work/mismatch.log" 2>&1; then exit 1; fi
grep -F "Local archive checksum mismatch for $tag_a:" "$work/mismatch.log"
cmp "$work/public-marker" "$app/.release/current"
preserved
old_path=release/v1.0.0/nginx/partnerconsole.conf
old="$app/$old_path"
cp -p "$old" "$work/original"
printf '\n# local edit\n' >> "$old"
if "${update_from_a[@]}" > "$work/edited.log" 2>&1; then exit 1; fi
grep -Fq 'installed files differ' "$work/edited.log"
grep -F "$old_path" "$work/edited.log"
cmp "$work/public-marker" "$app/.release/current"
[[ ! -e "$app/deploy" ]]
version_is 1.0.9
preserved
cp -p "$work/original" "$old"
personal="$app/release/v1.0.0/generated/personal.conf"
mkdir -p "$(dirname "$personal")"
printf 'keep personal file\n' > "$personal"
cp "$personal" "$work/personal.conf"
stat -c '%a:%u:%g' "$personal" > "$work/personal-metadata"
personal_unchanged() {
  cmp "$work/personal.conf" "$personal"
  stat -c '%a:%u:%g' "$personal" | cmp "$work/personal-metadata" -
}
"${update_from_a[@]}"
runtime "$tag_a" "$a"
[[ ! -s "$PC_RELEASE_FIXTURE_DIR/requests" ]]
archive_test_had_no_requests
preserved
for managed in env/backend.env.example env/frontend.env.example nginx/partnerconsole.conf requirements.lock.txt \
               systemd/partnerconsole-api.service systemd/partnerconsole-web.service; do
  [[ ! -e "$app/release/v1.0.0/$managed" && -f "$app/deploy/$managed" ]]
done
cmp "$work/original" "$app/deploy/nginx/partnerconsole.conf"
personal_unchanged
bash "$app/scripts/render-host-templates.sh" ppv1
grep -Fxq "WorkingDirectory=$app" "$app/deploy/generated/partnerconsole-api.service"
grep -Fxq "WorkingDirectory=$app" "$app/deploy/generated/partnerconsole-web.service"
cmp "$app/deploy/nginx/partnerconsole.conf" "$app/deploy/generated/partnerconsole.conf"
personal_unchanged
echo 'Public v1.0.9 to A via local archive: refused the locally edited managed template, moved managed files, and preserved the personal file, config, key, accounts, wallet, ledger and subscription.'
touch "$work/browser/updated"
wait_for_browser complete
wait "$probe_pid"
unset probe_pid
cat "$work/browser.log"

# Check the normal installed online command separately with the next fixture.
rm -- "$PC_RELEASE_FIXTURE_DIR/block-repository"
publish_fixture "$tag_b"
archive="$PC_RELEASE_FIXTURE_DIR/$tag_b/partnerconsole-$tag_b.tar.gz"
mv "$archive" "$work/good.tar.gz"
printf 'truncated release\n' > "$archive"
# Already root on this disposable runner, including the log redirect.
# shellcheck disable=SC2024
if sudo partnerconsole update > "$work/corrupt.log" 2>&1; then exit 1; fi
grep -Fq 'Application package checksum mismatch' "$work/corrupt.log"
runtime "$tag_a" "$a"
preserved
mv "$work/good.tar.gz" "$archive"
sudo partnerconsole update
runtime "$tag_b" "$b"
preserved
cp "$app/runtime/pids/api.id" "$work/api.id"
cp "$app/runtime/pids/web.id" "$work/web.id"
before_requests="$(wc -l < "$PC_RELEASE_FIXTURE_DIR/requests")"
sudo partnerconsole update
cmp "$app/runtime/pids/api.id" "$work/api.id"
cmp "$app/runtime/pids/web.id" "$work/web.id"
[[ "$(wc -l < "$PC_RELEASE_FIXTURE_DIR/requests")" == $((before_requests + 1)) ]]
preserved
personal_unchanged
echo 'A to B: rejected corrupt archive, updated via installed launcher, preserved data. Repeating Update did not download or restart.'

bash "$tools/backup-restore-linux-check.sh"
preserved
personal_unchanged
version_is "${tag_b#v}"
