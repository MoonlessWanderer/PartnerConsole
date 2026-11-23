#!/usr/bin/env bash
# Shared path and environment setup. Source this; do not execute it.
if [[ -z "${PP_ROOT:-}" ]]; then
  _pp_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ "$(basename "$_pp_source_dir")" == "scripts" ]]; then
    PP_ROOT="$(cd "$_pp_source_dir/.." && pwd)"
  else
    PP_ROOT="$_pp_source_dir"
  fi
  unset _pp_source_dir
fi

if [[ "$PP_ROOT" == *" "* || "$PP_ROOT" == *$'\n'* || "$PP_ROOT" == *$'\r'* ]]; then
  echo "The install folder path cannot contain spaces or line breaks." >&2
  return 1 2>/dev/null || exit 1
fi

pp_prepare_runtime() {
  umask 077
  mkdir -p \
    "$PP_ROOT/config" \
    "$PP_ROOT/data" \
    "$PP_ROOT/logs" \
    "$PP_ROOT/backups" \
    "$PP_ROOT/runtime/tmp" \
    "$PP_ROOT/runtime/cache/pip" \
    "$PP_ROOT/runtime/cache/npm" \
    "$PP_ROOT/runtime/cache/xdg" \
    "$PP_ROOT/runtime/cache/pyc" \
    "$PP_ROOT/runtime/pids"
  # Tighten private directories owned by this user. Config stays group-readable
  # so ppv1 can start after a root backup, but ppv1 still cannot write it.
  local private_dir env_file
  for private_dir in \
    "$PP_ROOT/data" \
    "$PP_ROOT/logs" \
    "$PP_ROOT/backups" \
    "$PP_ROOT/runtime" \
    "$PP_ROOT/runtime/tmp" \
    "$PP_ROOT/runtime/cache" \
    "$PP_ROOT/runtime/pids"
  do
    if [[ -O "$private_dir" ]]; then
      chmod 700 "$private_dir"
    fi
  done
  if [[ "$(id -u)" -eq 0 && -O "$PP_ROOT/config" ]]; then
    chmod 750 "$PP_ROOT/config"
    for env_file in "$PP_ROOT/config/api.env" "$PP_ROOT/config/web.env"; do
      if [[ -f "$env_file" && -O "$env_file" ]]; then
        chmod 640 "$env_file"
      fi
    done
  fi
  export TMPDIR="$PP_ROOT/runtime/tmp"
  export TMP="$TMPDIR"
  export TEMP="$TMPDIR"
  export XDG_CACHE_HOME="$PP_ROOT/runtime/cache/xdg"
  export XDG_CONFIG_HOME="$PP_ROOT/config"
  export PIP_CACHE_DIR="$PP_ROOT/runtime/cache/pip"
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export npm_config_cache="$PP_ROOT/runtime/cache/npm"
  export NPM_CONFIG_CACHE="$PP_ROOT/runtime/cache/npm"
  export npm_config_tmp="$PP_ROOT/runtime/tmp"
  export PYTHONPYCACHEPREFIX="$PP_ROOT/runtime/cache/pyc"
  export NEXT_TELEMETRY_DISABLED=1
}

pp_process_belongs_to_install() {
  local pid="$1"
  local uid cwd exe cmdline
  [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  uid="$(stat -c '%u' "/proc/$pid")" || return 1
  [[ "$uid" == "$(id -u)" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$PP_ROOT" || "$cwd" == "$PP_ROOT"/* ]] || return 1
  exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
  if [[ "$exe" == "$PP_ROOT"/* ]]; then
    return 0
  fi
  cmdline="$(tr '\0' '\n' < "/proc/$pid/cmdline" || true)"
  printf '%s\n' "$cmdline" | grep -F -q -- "$PP_ROOT/"
}

pp_stat_field() {
  local pid="$1" index="$2" stat rest
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/stat" ]] || return 1
  stat="$(<"/proc/$pid/stat")" || return 1
  rest="${stat##*)}"
  awk -v n="$index" 'NF >= n { print $n; found = 1 } END { exit(found ? 0 : 1) }' <<<"$rest"
}

pp_service_file() {
  printf '%s\n' "$PP_ROOT/runtime/pids/$1.id"
}

pp_load_service_identity() {
  local name="$1" file
  local -a record
  file="$(pp_service_file "$name")"
  [[ -f "$file" && ! -L "$file" ]] || return 1
  mapfile -t -n 5 record < "$file" || return 1
  [[ "${#record[@]}" == 4 ]] || return 1
  [[ "${record[0]}" =~ ^pid=[1-9][0-9]*$ && "${record[1]}" =~ ^starttime=[0-9]+$ &&
     "${record[2]}" =~ ^uid=[0-9]+$ && "${record[3]}" =~ ^pgid=[1-9][0-9]*$ ]] || return 1
  PP_ID_PID="${record[0]#*=}"
  PP_ID_START="${record[1]#*=}"
  PP_ID_UID="${record[2]#*=}"
  PP_ID_PGID="${record[3]#*=}"
}

pp_write_service_identity() {
  local name="$1" pid="$2" starttime uid pgid file
  starttime="$(pp_stat_field "$pid" 20)" || return 1
  uid="$(stat -c '%u' "/proc/$pid")" || return 1
  pgid="$(pp_stat_field "$pid" 3)" || return 1
  [[ "$uid" == "$(id -u)" ]] || return 1
  mkdir -p "$PP_ROOT/runtime/pids"
  file="$(pp_service_file "$name")"
  printf 'pid=%s\nstarttime=%s\nuid=%s\npgid=%s\n' "$pid" "$starttime" "$uid" "$pgid" > "$file"
  chmod 600 "$file"
  printf '%s\n' "$pid" > "$PP_ROOT/runtime/pids/${name}.pid"
  chmod 600 "$PP_ROOT/runtime/pids/${name}.pid"
}

pp_clear_service_identity() {
  local name="$1"
  rm -f "$PP_ROOT/runtime/pids/${name}.pid" "$(pp_service_file "$name")"
}

pp_cwd_in_install() {
  local pid="$1" cwd
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ -n "$cwd" && ( "$cwd" == "$PP_ROOT" || "$cwd" == "$PP_ROOT"/* ) ]]
}

pp_pid_in_service() {
  local name="$1" pid="$2" expected_uid="${3:-$(id -u)}" start uid pgid
  pp_load_service_identity "$name" || return 1
  [[ "$pid" =~ ^[0-9]+$ && "$pid" != "1" && -r "/proc/$pid/stat" ]] || return 1
  start="$(pp_stat_field "$pid" 20)" || return 1
  uid="$(stat -c '%u' "/proc/$pid")" || return 1
  pgid="$(pp_stat_field "$pid" 3)" || return 1
  [[ "$uid" == "$PP_ID_UID" && "$uid" == "$expected_uid" ]] || return 1
  [[ "$pgid" == "$PP_ID_PGID" ]] || return 1
  pp_cwd_in_install "$pid" || return 1
  if [[ "$pid" == "$PP_ID_PID" && "$start" != "$PP_ID_START" ]]; then
    return 1
  fi
}

pp_leader_matches() {
  local name="$1"
  pp_load_service_identity "$name" || return 1
  pp_pid_in_service "$name" "$PP_ID_PID" "${2:-}"
}

# The management launcher has permission to see every socket owner. Require
# the expected service UID, a valid leader, and ownership of every listener.
# 0 = running, 1 = stopped, 2 = unsafe or still starting/stopping.
pp_service_state() {
  local name="$1" port="$2" expected_uid="$3" identity pidfile listeners owners pid leader
  identity="$(pp_service_file "$name")"
  pidfile="$PP_ROOT/runtime/pids/$name.pid"
  [[ ! -L "$identity" && ! -L "$pidfile" ]] || return 2
  listeners="$(ss -H -lntp "sport = :$port" 2>/dev/null)" || return 2
  if [[ ! -e "$identity" && ! -e "$pidfile" && -z "$listeners" ]]; then
    return 1
  fi
  [[ -f "$identity" && -f "$pidfile" && -n "$listeners" ]] || return 2
  [[ "$(stat -c '%u:%a' "$identity")" == "$expected_uid:600" &&
     "$(stat -c '%u:%a' "$pidfile")" == "$expected_uid:600" ]] || return 2
  pp_leader_matches "$name" "$expected_uid" || return 2
  leader="$PP_ID_PID"
  [[ "$leader" == "$PP_ID_PGID" && "$leader" == "$(<"$pidfile")" &&
     "$(pp_stat_field "$leader" 4)" == "$leader" ]] || return 2
  owners="$(awk -v port="$port" '
    $1 != "LISTEN" || $4 != "127.0.0.1:" port || !/pid=[0-9]+/ {exit 1}
    {
      line = $0
      while (match(line, /pid=[0-9]+/)) {
        print substr(line, RSTART + 4, RLENGTH - 4)
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' <<<"$listeners")" || return 2
  [[ -n "$owners" ]] || return 2
  while IFS= read -r pid; do
    pp_pid_in_service "$name" "$pid" "$expected_uid" || return 2
  done <<<"$owners"
  pp_leader_matches "$name" "$expected_uid" || return 2
  [[ "$PP_ID_PID" == "$leader" ]] || return 2
}

pp_port_listening() {
  local port="$1"
  command -v ss >/dev/null 2>&1 || return 1
  ss -lnt | awk -v port="$port" '
    {
      for (i = 1; i <= NF; i++) if ($i ~ (":" port "$")) found = 1
    }
    END { exit(found ? 0 : 1) }
  '
}

pp_listening_pids() {
  local port="$1"
  command -v ss >/dev/null 2>&1 || return 1
  ss -lntp 2>/dev/null | awk -v port="$port" '
    {
      hit = 0
      for (i = 1; i <= NF; i++) if ($i ~ (":" port "$")) hit = 1
      if (!hit) next
      line = $0
      while (match(line, /pid=[0-9]+/)) {
        print substr(line, RSTART + 4, RLENGTH - 4)
        line = substr(line, RSTART + RLENGTH)
      }
    }
  '
}

pp_port_owned_by_service() {
  local name="$1" port="$2" pid
  pp_port_listening "$port" || return 1
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    if pp_pid_in_service "$name" "$pid"; then
      return 0
    fi
  done < <(pp_listening_pids "$port")
  return 1
}

pp_stop_service() {
  local name="$1" victim _
  if [[ ! -f "$PP_ROOT/runtime/pids/${name}.pid" && ! -f "$(pp_service_file "$name")" ]]; then
    echo "$name is not running."
    return 0
  fi
  if ! pp_leader_matches "$name"; then
    pp_clear_service_identity "$name"
    echo "$name is not running."
    return 0
  fi
  for victim in /proc/[0-9]*; do
    victim="${victim#/proc/}"
    [[ "$victim" =~ ^[0-9]+$ && "$victim" != "$$" ]] || continue
    if pp_pid_in_service "$name" "$victim"; then
      kill "$victim" 2>/dev/null || true
    fi
  done
  for _ in $(seq 1 20); do
    pp_leader_matches "$name" || break
    sleep 0.2
  done
  if pp_leader_matches "$name"; then
    for victim in /proc/[0-9]*; do
      victim="${victim#/proc/}"
      [[ "$victim" =~ ^[0-9]+$ && "$victim" != "$$" ]] || continue
      if pp_pid_in_service "$name" "$victim"; then
        kill -KILL "$victim" 2>/dev/null || true
      fi
    done
  fi
  if pp_leader_matches "$name"; then
    echo "$name did not stop." >&2
    return 1
  fi
  pp_clear_service_identity "$name"
  echo "$name stopped."
}

pp_launch_service() {
  local name="$1" pid _ 
  shift
  if pp_leader_matches "$name"; then
    echo "$name is already running (${PP_ID_PID})."
    return 0
  fi
  pp_clear_service_identity "$name"
  mkdir -p "$PP_ROOT/logs" "$PP_ROOT/runtime/pids"
  touch "$PP_ROOT/logs/${name}.log"
  chmod 600 "$PP_ROOT/logs/${name}.log"
  setsid "$@" >> "$PP_ROOT/logs/${name}.log" 2>&1 < /dev/null &
  pid=$!
  # Record the identity only after setsid has made the child its own group
  # leader, so the recorded process group stays valid.
  for _ in $(seq 1 50); do
    if [[ "$(pp_stat_field "$pid" 3 2>/dev/null)" == "$pid" ]]; then
      break
    fi
    sleep 0.1
  done
  if [[ "$(pp_stat_field "$pid" 3 2>/dev/null)" != "$pid" ]] || ! pp_write_service_identity "$name" "$pid"; then
    kill "$pid" 2>/dev/null || true
    echo "$name did not stay running." >&2
    return 1
  fi
  echo "$name started ($pid)."
}

pp_claim_service() {
  local name="$1" port="$2"
  shift 2
  if pp_leader_matches "$name"; then
    echo "$name is already running (${PP_ID_PID})."
    return 0
  fi
  if pp_port_listening "$port"; then
    echo "Port ${port} is in use by another program. This install was not started." >&2
    return 1
  fi
  pp_launch_service "$name" "$@"
}

pp_http_matches() {
  local url="$1" mode="$2" body code src status
  body="$(mktemp)"
  if [[ "$mode" == "web" ]]; then
    code="$(curl -sS --max-time 5 -H 'Cookie: partnerconsole=1' -o "$body" -w '%{http_code}' "$url" || true)"
  else
    code="$(curl -sS --max-time 5 -o "$body" -w '%{http_code}' "$url" || true)"
  fi
  if [[ "$code" != "200" ]]; then
    rm -f "$body"
    return 1
  fi
  if [[ "$mode" == "api" ]]; then
    grep -q '"ok"[[:space:]]*:[[:space:]]*true' "$body"
    status=$?
    rm -f "$body"
    return "$status"
  fi
  if grep -q 'Internal Server Error' "$body" || grep -q 'Application error' "$body"; then
    rm -f "$body"
    return 1
  fi
  if ! grep -q 'ورود' "$body"; then
    rm -f "$body"
    return 1
  fi
  src="$(grep -oE '/_next/static/[^"[:space:]]+\.js' "$body" | head -n 1)"
  rm -f "$body"
  [[ -n "$src" ]] || return 1
  code="$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:3741${src}" || true)"
  [[ "$code" == "200" ]]
}

pp_wait_for_ready() {
  local name="$1" port="$2" url="$3" mode="$4"
  for _ in $(seq 1 30); do
    if ! pp_leader_matches "$name"; then
      echo "$name exited before it was ready." >&2
      return 1
    fi
    if pp_port_owned_by_service "$name" "$port" && pp_http_matches "$url" "$mode"; then
      return 0
    fi
    sleep 1
  done
  echo "$name did not become ready." >&2
  return 1
}

pp_abort_started_services() {
  pp_stop_service web >/dev/null 2>&1 || true
  pp_stop_service api >/dev/null 2>&1 || true
  printf '%s\n' "$1" >&2
  return 1
}

pp_load_env_file() {
  local file="$1"
  local line key value
  [[ -f "$file" ]] || { echo "Missing $file" >&2; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      echo "Invalid line in $file" >&2
      return 1
    fi
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" == *" "* || "$value" == *"#"* || "$value" == *"'"* || "$value" == *'"'* ]]; then
      echo "Value for $key must be one token without spaces, quotes, or #." >&2
      return 1
    fi
    export "$key=$value"
  done < "$file"
}

pp_reject_placeholders() {
  local file value
  for file in "$PP_ROOT/config/api.env" "$PP_ROOT/config/web.env"; do
    [[ -f "$file" ]] || continue
    value="$(tr -d '\r' < "$file")"
    if [[ "$value" == *"REPLACE_WITH"* || "$value" == *"panel.example.com"* ]]; then
      echo "Edit $file and replace every placeholder before this command." >&2
      return 1
    fi
  done
  if [[ "${DATABASE_URL:-}" == "sqlite:///./data/partner.db" || "${DATABASE_URL:-}" == *"backend/data/partner.db" ]]; then
    echo "Refusing the development SQLite file. Choose data/partnerconsole.db inside this install, or a MySQL schema you created for it." >&2
    return 1
  fi
}

pp_describe_database() {
  local python="$PP_ROOT/backend/.venv/bin/python"
  [[ -x "$python" ]] || { echo "Run bash install.sh first." >&2; return 1; }
  PP_ROOT="$PP_ROOT" DATABASE_URL="${DATABASE_URL:-}" "$python" - <<'PY'
import os
from urllib.parse import unquote, urlparse
raw = os.environ.get("DATABASE_URL", "")
if raw.startswith("sqlite:"):
    path = raw.removeprefix("sqlite:///")
    print(f"database=sqlite path={path}")
elif raw.startswith("mysql+pymysql://"):
    parts = urlparse(raw)
    name = unquote(parts.path.lstrip("/").split("?", 1)[0])
    print(f"database=mysql host={parts.hostname} port={parts.port or 3306} name={name}")
else:
    print("database=unsupported")
    raise SystemExit(2)
PY
}

pp_confirm_database() {
  pp_describe_database
  if [[ "${1:-}" != "--confirm-database" ]]; then
    echo "No database was changed. Run again with --confirm-database after the line above is the database you intend." >&2
    return 1
  fi
}

pp_venv_python() {
  local python="$PP_ROOT/backend/.venv/bin/python"
  [[ -x "$python" ]] || { echo "Run bash install.sh first." >&2; return 1; }
  local prefix
  prefix="$("$python" -c "import sys; print(sys.prefix)")" || return 1
  if [[ "$prefix" != "$PP_ROOT/backend/.venv" ]]; then
    echo "The virtual environment belongs to another folder. Run bash install.sh again." >&2
    return 1
  fi
  printf '%s\n' "$python"
}
