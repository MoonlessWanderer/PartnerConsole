#!/usr/bin/env bash
# Only sourced by tests on disposable Ubuntu runners.
feed=/srv/pc-release-fixture
setup_release_feed() {
  [[ ! -e "$feed" && ! -e /etc/sudoers.d/pc-release-fixture ]]
  install -d -m 700 "$feed/bin"
  cat > "$feed/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
feed=/srv/pc-release-fixture
url="${@: -1}"
case "$url" in
  https://api.github.com/repos/MoonlessWanderer/PartnerConsole/releases/latest)
    file="$feed/latest.json" ;;
  https://github.com/MoonlessWanderer/PartnerConsole/releases/latest/download/*)
    file="$feed/$(cat "$feed/latest")/${url##*/}" ;;
  https://github.com/MoonlessWanderer/PartnerConsole/releases/download/*)
    file="$feed/${url#*releases/download/}" ;;
  *)
    exec /usr/bin/curl "$@" ;;
esac
output=""
args=("$@")
for ((i=0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == "-o" ]]; then output="${args[i+1]}"; break; fi
done
printf '%s\n' "$url" >> "$feed/requests"
cp -- "$file" "${output:-${url##*/}}"
EOF
  chmod 700 "$feed/bin/curl"
  printf 'Defaults:root secure_path="%s:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n' "$feed/bin" > /etc/sudoers.d/pc-release-fixture
  chmod 440 /etc/sudoers.d/pc-release-fixture
  visudo -c -f /etc/sudoers.d/pc-release-fixture
  export PATH="$feed/bin:$PATH"
}
publish_fixture() {
  local tag="$1"
  (cd "$feed/$tag" && sha256sum --check --strict SHA256SUMS)
  printf '%s\n' "$tag" > "$feed/latest"
  python3 - "$feed" "$tag" <<'PY'
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
