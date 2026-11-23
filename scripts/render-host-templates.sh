#!/usr/bin/env bash
# Render host templates inside this install. It does not copy them anywhere else.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
pp_prepare_runtime
service_user="${1:-$(id -un)}"
if [[ ! "$service_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "Service user must be a plain account name." >&2
  exit 1
fi
out="$PP_ROOT/release/v1.0.0/generated"
mkdir -p "$out"
chmod 700 "$out"
touch "$PP_ROOT/logs/api.log" "$PP_ROOT/logs/web.log"
chmod 600 "$PP_ROOT/logs/api.log" "$PP_ROOT/logs/web.log"
PP_ROOT="$PP_ROOT" SERVICE_USER="$service_user" OUT="$out" python3 - <<'PY'
import os
from pathlib import Path
root = Path(os.environ["PP_ROOT"])
out = Path(os.environ["OUT"])
repl = {"@ROOT@": str(root), "@SERVICE_USER@": os.environ["SERVICE_USER"]}
for name in (
    "systemd/partnerconsole-api.service",
    "systemd/partnerconsole-web.service",
    "nginx/partnerconsole.conf",
):
    text = (root / "release" / "v1.0.0" / name).read_text(encoding="utf-8")
    for key, value in repl.items():
        text = text.replace(key, value)
    target = out / Path(name).name
    target.write_text(text, encoding="utf-8", newline="\n")
    print(target)
PY
echo "Templates were written only inside this install. Nothing was copied to the host service manager or web server."
