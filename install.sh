#!/usr/bin/env bash
set -euo pipefail
PP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PP_ROOT"
source "$PP_ROOT/scripts/common.sh"
pp_prepare_runtime

local_database=0
if [[ "${1:-}" == "--local-database" ]]; then
  local_database=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: bash install.sh [--local-database]" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1 || ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "Python 3.12+, Node.js 22+, and npm must already be on PATH." >&2
  echo "This installer does not install system packages, systemd units, or nginx." >&2
  exit 1
fi
python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
node -e 'const major=Number(process.versions.node.split(".")[0]); if (!(major>=22)) process.exit(1)'

if [[ ! -f "$PP_ROOT/config/api.env" ]]; then
  PP_ROOT="$PP_ROOT" LOCAL_DATABASE="$local_database" python3 - <<'PY'
import os
import secrets
from pathlib import Path
root = Path(os.environ["PP_ROOT"])
source = (root / "config" / "api.env.example").read_text(encoding="utf-8")
lines = []
for line in source.splitlines():
    if line.startswith(("SECRET_KEY=", "ADMIN_JWT_SECRET=", "PARTNER_JWT_SECRET=")):
        key = line.split("=", 1)[0]
        lines.append(f"{key}={secrets.token_urlsafe(48)}")
    elif line.startswith("DATABASE_URL=") and os.environ.get("LOCAL_DATABASE") == "1":
        lines.append(f"DATABASE_URL=sqlite:///{root / 'data' / 'partnerconsole.db'}")
    else:
        lines.append(line)
target = root / "config" / "api.env"
target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
os.chmod(target, 0o600)
PY
  echo "Created config/api.env with new signing keys. Set the database URL and CORS origin before bootstrap. --local-database only fills a SQLite URL inside this folder, and only when this file is created."
else
  echo "Keeping the existing config/api.env."
  if [[ "$local_database" == "1" ]]; then
    echo "--local-database was not applied because config/api.env already exists."
  fi
fi

if [[ ! -f "$PP_ROOT/config/web.env" ]]; then
  umask 077
  cp "$PP_ROOT/config/web.env.example" "$PP_ROOT/config/web.env"
  chmod 600 "$PP_ROOT/config/web.env"
fi
chmod 600 "$PP_ROOT/config/api.env" "$PP_ROOT/config/web.env"

venv="$PP_ROOT/backend/.venv"
if [[ -x "$venv/bin/python" ]]; then
  prefix="$("$venv/bin/python" -c "import sys; print(sys.prefix)")" || prefix=""
  if [[ "$prefix" != "$venv" ]]; then
    rm -rf "$venv"
  fi
fi
if [[ ! -x "$venv/bin/python" ]]; then
  python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -e "$PP_ROOT/backend" -c "$PP_ROOT/release/v1.0.0/requirements.lock.txt"
npm ci --prefix "$PP_ROOT"

if [[ "$(tr -d '\r' < "$PP_ROOT/config/web.env")" == *"REPLACE_WITH"* || "$(tr -d '\r' < "$PP_ROOT/config/web.env")" == *"panel.example.com"* ]]; then
  echo "Web build skipped. Set NEXT_PUBLIC_API_BASE in config/web.env, then run bash install.sh again."
else
  pp_load_env_file "$PP_ROOT/config/web.env"
  npm run build --prefix "$PP_ROOT"
fi

if [[ ! -f "$PP_ROOT/node_modules/next/dist/compiled/cookie/index.js" || ! -f "$PP_ROOT/node_modules/next/dist/compiled/cookie/package.json" ]]; then
  echo "Next.js did not install next/dist/compiled/cookie." >&2
  exit 1
fi
(
  cd "$PP_ROOT" || exit 1
  node -e "require('next/dist/compiled/cookie')"
) || {
  echo "Next.js could not load next/dist/compiled/cookie." >&2
  exit 1
}

chmod 755 \
  "$PP_ROOT/install.sh" \
  "$PP_ROOT/start.sh" \
  "$PP_ROOT/stop.sh" \
  "$PP_ROOT/restart.sh" \
  "$PP_ROOT/status.sh" \
  "$PP_ROOT/partnerconsole.sh" \
  "$PP_ROOT"/scripts/*.sh

echo "Install folder: $PP_ROOT"
echo "No system package was installed and no file was written outside this folder."
echo "Next: edit config/api.env, run bash scripts/bootstrap.sh --confirm-database, then bash start.sh."
