#!/usr/bin/env bash
# Only sourced by tests on disposable Ubuntu runners.
PC_RELEASE_FIXTURE_DIR=/srv/pc-release-fixture
setup_release_feed() {
  [[ ! -e "$PC_RELEASE_FIXTURE_DIR" && ! -e /etc/sudoers.d/pc-release-fixture ]]
  install -d -m 700 "$PC_RELEASE_FIXTURE_DIR/bin"
  cat > "$PC_RELEASE_FIXTURE_DIR/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
PC_RELEASE_FIXTURE_DIR=/srv/pc-release-fixture
url="${@: -1}"
if [[ -f "$PC_RELEASE_FIXTURE_DIR/block-repository" ]]; then
  case "$url" in
    https://github.com/MoonlessWanderer/PartnerConsole*|https://api.github.com/repos/MoonlessWanderer/PartnerConsole*|https://codeload.github.com/MoonlessWanderer/PartnerConsole*)
      printf '%s\n' "$url" >> "$PC_RELEASE_FIXTURE_DIR/blocked-requests"
      echo "PartnerConsole repository requests are blocked by the test fixture." >&2
      exit 97 ;;
  esac
fi
case "$url" in
  https://api.github.com/repos/MoonlessWanderer/PartnerConsole/releases/latest)
    file="$PC_RELEASE_FIXTURE_DIR/latest.json" ;;
  https://github.com/MoonlessWanderer/PartnerConsole/releases/latest/download/*)
    file="$PC_RELEASE_FIXTURE_DIR/$(cat "$PC_RELEASE_FIXTURE_DIR/latest")/${url##*/}" ;;
  https://github.com/MoonlessWanderer/PartnerConsole/releases/download/*)
    file="$PC_RELEASE_FIXTURE_DIR/${url#*releases/download/}" ;;
  *)
    exec /usr/bin/curl "$@" ;;
esac
output=""
args=("$@")
for ((i=0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == "-o" ]]; then output="${args[i+1]}"; break; fi
done
printf '%s\n' "$url" >> "$PC_RELEASE_FIXTURE_DIR/requests"
cp -- "$file" "${output:-${url##*/}}"
EOF
  chmod 700 "$PC_RELEASE_FIXTURE_DIR/bin/curl"
  printf 'Defaults:root secure_path="%s:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n' "$PC_RELEASE_FIXTURE_DIR/bin" > /etc/sudoers.d/pc-release-fixture
  chmod 440 /etc/sudoers.d/pc-release-fixture
  visudo -c -f /etc/sudoers.d/pc-release-fixture
  export PATH="$PC_RELEASE_FIXTURE_DIR/bin:$PATH"
}
block_release_feed() {
  local url=https://api.github.com/repos/MoonlessWanderer/PartnerConsole/releases/latest
  touch "$PC_RELEASE_FIXTURE_DIR/block-repository"
  if sudo curl -fsS -o /dev/null "$url"; then
    echo 'The archive test could still reach the repository.' >&2
    return 1
  fi
  grep -Fxq "$url" "$PC_RELEASE_FIXTURE_DIR/blocked-requests"
  : > "$PC_RELEASE_FIXTURE_DIR/blocked-requests"
}
archive_test_had_no_requests() {
  if [[ -s "$PC_RELEASE_FIXTURE_DIR/blocked-requests" ]]; then
    cat "$PC_RELEASE_FIXTURE_DIR/blocked-requests" >&2
    return 1
  fi
  echo 'No requests to the PartnerConsole repository or release feed were attempted.'
}
publish_fixture() {
  local tag="$1"
  (cd "$PC_RELEASE_FIXTURE_DIR/$tag" && sha256sum --check --strict SHA256SUMS)
  printf '%s\n' "$tag" > "$PC_RELEASE_FIXTURE_DIR/latest"
  python3 - "$PC_RELEASE_FIXTURE_DIR" "$tag" <<'PY'
import hashlib, json, sys
from pathlib import Path
root, tag = Path(sys.argv[1]), sys.argv[2]
assets=[]
for name in ("partnerconsole.sh", f"partnerconsole-{tag}.tar.gz", "SHA256SUMS"):
    digest = hashlib.sha256((root/tag/name).read_bytes()).hexdigest()
    assets.append({"name": name, "digest": "sha256:"+digest, "state": "uploaded"})
(root/"latest.json").write_text(json.dumps({"tag_name":tag, "draft":False, "prerelease":False, "assets":assets}))
PY
}
