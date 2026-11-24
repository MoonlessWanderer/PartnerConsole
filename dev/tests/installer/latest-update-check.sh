#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
export FIXTURE="$work" PARTNERCONSOLE_ROOT="$work/installed"
installed="$PARTNERCONSOLE_ROOT/partnerconsole.sh"
mkdir -p "$PARTNERCONSOLE_ROOT"/{.release,config} "$work/bin"
touch "$PARTNERCONSOLE_ROOT/config/api.env"
printf 'tag=v1.9.0\n' > "$PARTNERCONSOLE_ROOT/.release/current"

# Exercise both public dispatch paths. Stub host privileges, the TTY and the deployment
# payload so the checks also run without root on a developer machine.
cat > "$work/overrides" <<'EOF'
require_root() { :; }
validate_root() { :; }
assert_root_file() { :; }
tty_read() { read -r "${@: -1}"; }
pause_menu() { :; }
update_app() {
  printf '%s %s\n' "$RELEASE_TAG" "$APP_ROOT" >> "$FIXTURE/applied"
  if [[ ! "${BASH_SOURCE[0]}" -ef "$APP_ROOT/partnerconsole.sh" ]]; then
    cp "${BASH_SOURCE[0]}" "$APP_ROOT/partnerconsole.sh"
  fi
  printf 'tag=%s\n' "$RELEASE_TAG" > "$APP_ROOT/.release/current"
}
EOF
awk -v extra="$work/overrides" '/^if \[\[ "\$\{PARTNERCONSOLE_LIBRARY/ {
  while ((getline line < extra) > 0) print line
  close(extra)
} {print}' "$repo/partnerconsole.sh" > "$work/template"
sed 's/^RELEASE_TAG=.*/RELEASE_TAG="v1.9.0"/' "$work/template" > "$work/old.sh"
sed 's/^RELEASE_TAG=.*/RELEASE_TAG="v1.10.0"/' "$work/template" > "$work/asset.sh"
cp "$work/old.sh" "$installed"

cat > "$work/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -eu
while [[ "$1" != -o ]]; do shift; done
case "$3" in
  https://api.github.com/repos/MoonlessWanderer/PartnerConsole/releases/latest)
    cp "$FIXTURE/latest.json" "$2" ;;
  https://github.com/MoonlessWanderer/PartnerConsole/releases/download/v1.10.0/partnerconsole.sh)
    cp "$FIXTURE/asset.sh" "$2" ;;
  *) exit 1 ;;
esac
EOF
chmod +x "$work/bin/curl"
export PATH="$work/bin:$PATH"
asset_hash="$(sha256sum "$work/asset.sh" | cut -d ' ' -f1)"
metadata() {
  printf '{"tag_name":"%s","draft":false,"prerelease":%s,"assets":[{"name":"partnerconsole.sh","state":"uploaded","digest":"sha256:%s"},{"name":"partnerconsole-%s.tar.gz","state":"uploaded","digest":"sha256:%s"},{"name":"SHA256SUMS","state":"uploaded","digest":"sha256:%s"}]}\n' \
    "$1" "${2:-false}" "${3:-$asset_hash}" "$1" "$asset_hash" "$asset_hash" > "$work/latest.json"
}
reject() {
  if bash "$installed" update > "$work/message" 2>&1; then
    echo "Unexpected success for $1" >&2
    exit 1
  fi
  grep -Fq "$2" "$work/message"
  [[ ! -e "$work/applied" ]]
  cmp "$installed" "$work/old.sh"
}

reject 'discovery failure' 'Could not retrieve'
printf 'invalid-json\n' > "$work/latest.json"
reject 'invalid metadata' 'Invalid GitHub release metadata'
printf '{"tag_name":"v1.10.0","draft":false,"prerelease":false,"assets":[]}\n' > "$work/latest.json"
reject 'missing assets' 'one uploaded partnerconsole-v1.10.0.tar.gz'
metadata v1.8.9
reject 'older release' 'Refusing to downgrade'
metadata v1.10.0 true
reject 'prerelease' 'stable Partner Console version'
metadata v1.10.0 false invalid
reject 'missing digest' 'SHA256 digest'
metadata v1.10.0 false "$(printf '%064d' 0)"
reject 'corrupt installer' 'checksum mismatch'
metadata v1.10.0
mv "$work/asset.sh" "$work/good.sh"
reject 'download failure' 'Could not download'
cp "$work/old.sh" "$work/asset.sh"
metadata v1.10.0 false "$(sha256sum "$work/asset.sh" | cut -d ' ' -f1)"
reject 'tag mismatch' 'Installer tag mismatch'
mv "$work/good.sh" "$work/asset.sh"
metadata v1.10.0
(
  flock -x 9
  reject 'concurrent update' 'Another Update'
) 9< "$PARTNERCONSOLE_ROOT"

bash "$installed" update
grep -Fxq "v1.10.0 $PARTNERCONSOLE_ROOT" "$work/applied"
[[ "$(wc -l < "$work/applied")" == 1 ]]
cmp "$installed" "$work/asset.sh"
rm "$work/asset.sh"
bash "$installed" update  # Same tag uses the installed integrity checks.
[[ "$(wc -l < "$work/applied")" == 2 ]]

cp "$installed" "$work/asset.sh"
cp "$work/old.sh" "$installed"
printf 'tag=v1.9.0\n' > "$PARTNERCONSOLE_ROOT/.release/current"
printf '5\n0\n' | bash "$installed" menu > "$work/menu"
grep -Fxq 'Release: v1.10.0' "$work/menu"
[[ "$(wc -l < "$work/applied")" == 3 ]]
rm "$work/latest.json"
bash "$work/asset.sh" update  # An explicitly downloaded release needs no discovery.
[[ "$(wc -l < "$work/applied")" == 4 ]]
[[ -z "$(find "$PARTNERCONSOLE_ROOT/.release" -name 'update.*' -print -quit)" ]]
# A historical RC marker can be upgraded to the stable version with the same number.
printf 'tag=v1.10.0-rc.7\n' > "$PARTNERCONSOLE_ROOT/.release/current"
bash "$work/asset.sh" update
grep -Fxq 'tag=v1.10.0' "$PARTNERCONSOLE_ROOT/.release/current"
[[ "$(wc -l < "$work/applied")" == 5 ]]
echo 'ok latest stable CLI and menu, digest verification, pinned update and failure handling'
