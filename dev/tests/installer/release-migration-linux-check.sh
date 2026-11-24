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
dpkg --compare-versions "${tag_a#v}" gt 1.0.8
dpkg --compare-versions "${tag_b#v}" gt "${tag_a#v}"
expected="$(git -c safe.directory="$repo" -C "$repo" rev-parse HEAD)"
app=/srv/partnerconsole
[[ ! -e "$app" && ! -e /usr/local/bin/partnerconsole ]]
work="$(mktemp -d)"
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$work/password"
mkdir "$work/legacy"
(
  cd "$work/legacy"
  /usr/bin/curl --fail --silent --show-error --location --remote-name \
    https://github.com/MoonlessWanderer/PartnerConsole/releases/download/v1.0.8/partnerconsole.sh
  printf '%s  %s\n' 0753c1b7d88f11c33cb44bc2302e32dc9f5f1b05908192d4a3f9b5a15f8d2f52 partnerconsole.sh | sha256sum --check --strict
  python3 "$tools/release-tty.py" "$work/password" bash ./partnerconsole.sh install
)
grep -Fxq 'commit=154fb568d79a95dd9d85076f0a310e3bf0b55497' "$app/.release/current"
# shellcheck source=/dev/null
source "$tools/release-http-fixture.sh"
setup_release_feed
cp -r "$a" "$PC_RELEASE_FIXTURE_DIR/$tag_a"
cp -r "$b" "$PC_RELEASE_FIXTURE_DIR/$tag_b"
cat > "$PC_RELEASE_FIXTURE_DIR/bin/git" <<'EOF'
#!/bin/bash
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
trap 'kill "$!" 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18788/_lab/health >/dev/null 2>&1; then break; fi
  sleep 1
done
sample_state() { "$app/backend/.venv/bin/python" "$tools/release-migration-data.py" "$1" "$work/password"; }
sample_state seed > "$work/before.json"
preserved() {
  cmp "$sysconfig/api.env" "$app/config/api.env"
  cmp "$sysconfig/web.env" "$app/config/web.env"
  sample_state check > "$work/after.json"
  cmp "$work/before.json" "$work/after.json"
}
runtime() {
  local tag="$1" assets="$2" archive_sha
  grep -Fxq "tag=$tag" "$app/.release/current"
  grep -Fxq "commit=$expected" "$app/.release/current"
  read -r archive_sha _ < <(sha256sum "$assets/partnerconsole-$tag.tar.gz")
  grep -Fx "archive=$archive_sha" "$app/.release/current"
  cmp "$assets/partnerconsole.sh" "$app/partnerconsole.sh"
}
publish_fixture "$tag_a"
old="$app/dev/docs/releases/v1.0.8/RELEASE_NOTES.md"
cp -p "$old" "$work/original"
printf 'local edit\n' >> "$old"
# Already root on this disposable runner, including the log redirect.
# shellcheck disable=SC2024
if sudo partnerconsole update > "$work/edited.log" 2>&1; then exit 1; fi
grep -Fq 'installed files differ' "$work/edited.log"
grep -Fq 'dev/docs/releases/v1.0.8/RELEASE_NOTES.md' "$work/edited.log"
grep -Fxq tag=v1.0.8 "$app/.release/current"
preserved
cp -p "$work/original" "$old"
printf 'keep personal file\n' > "$app/dev/docs/personal.txt"
sudo partnerconsole update
runtime "$tag_a" "$a"
preserved
[[ ! -e "$old" && ! -e "$app/dev/v2lab/server.py" && ! -e "$app/.github/workflows/financial-safety.yml" ]]
grep -Fxq 'keep personal file' "$app/dev/docs/personal.txt"
echo 'v1.0.8 to A: stopped on local edit; removed unchanged managed development files; preserved personal file, config, encryption key, wallet, ledger and subscription'
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
grep -Fxq 'keep personal file' "$app/dev/docs/personal.txt"
echo 'A to B: rejected corrupt archive, updated via installed launcher, preserved data. Repeating Update did not download or restart.'
