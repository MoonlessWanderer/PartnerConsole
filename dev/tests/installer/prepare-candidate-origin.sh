#!/usr/bin/env bash
# Seal the exact CI checkout in a private repository, without updating a public tag.
set -euo pipefail
source_repo="$1"
origin="$2"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
git -c safe.directory="$source_repo" clone --quiet --no-local --no-tags "$source_repo" "$work/checkout"
cd "$work/checkout"
git checkout --quiet --detach "$(git -c safe.directory="$source_repo" -C "$source_repo" rev-parse HEAD)"
export PARTNERCONSOLE_LIBRARY=1
# shellcheck disable=SC1091
source ./partnerconsole.sh
pin="$(tree_sha256 "$PWD")"
if [[ "$pin" != "$RELEASE_TREE_SHA256" ]]; then
  sed -i "s/^RELEASE_TREE_SHA256=.*/RELEASE_TREE_SHA256=\"$pin\"/" partnerconsole.sh
  git add partnerconsole.sh
  git -c user.name=Fixture -c user.email=fixture@example.invalid commit -qm 'Seal the private CI fixture'
fi
git tag "$RELEASE_TAG"
git clone --quiet --bare --no-local "$PWD" "$origin"
