#!/usr/bin/env bash
# Installer template. Build it with dev/release/build.py.
# The app runs as ppv1. Root never executes a file ppv1 can write.
set -euo pipefail
umask 077

RELEASE_TAG=""
RELEASE_SOURCE_COMMIT=""
RELEASE_ARCHIVE_SHA256=""
OFFICIAL_RELEASES="https://github.com/MoonlessWanderer/PartnerConsole/releases"
APP_USER="ppv1"
DB_NAME="partnerconsole"
DB_USER="partnerconsole"
DEFAULT_ROOT="/srv/partnerconsole"
NODE_VERSION="22.23.5"
NODE_SHA256="cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8"
MYSQL_DEB_URL="https://dev.mysql.com/get/mysql-apt-config_0.8.40-1_all.deb"
MYSQL_DEB_SHA256="41a796dd6ea681aef4675d3b0b27924873d4f9777e524facf7693eabdf232e93"

log() { printf '%s\n' "$*" >&2; }
die() { printf '%s\n' "$*" >&2; exit 1; }

pp_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
      command "$candidate" "$@"
      return
    fi
  done
  die "Python is required."
}

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "Run this command with sudo."
}

need_tty() {
  [[ -r /dev/tty ]] || die "The menu needs an SSH terminal."
}

tty_read() {
  need_tty
  read -r "$@" </dev/tty
}

tty_silent() {
  need_tty
  read -r -s "$@" </dev/tty
  printf '\n' >/dev/tty
}

pause_menu() {
  tty_read -p "Press Enter to return to the menu... " _
}

nonroot_can_write_mode() {
  local mode="$1"
  [[ "$mode" =~ ^[0-7]+$ ]] || return 0
  [[ $((8#$mode & 022)) -ne 0 ]]
}

assert_root_file() {
  local path="$1"
  local uid mode parent
  [[ -e "$path" ]] || die "Missing $path."
  [[ ! -L "$path" ]] || die "Refusing symbolic link $path."
  uid="$(stat -c '%u' "$path")"
  mode="$(stat -c '%a' "$path")"
  [[ "$uid" == "0" ]] || die "Refusing $path because root does not own it."
  if nonroot_can_write_mode "$mode"; then
    die "Refusing $path because someone other than root can write it."
  fi
  parent="$(dirname "$path")"
  while [[ "$parent" != "/" && -n "$parent" ]]; do
    uid="$(stat -c '%u' "$parent")"
    mode="$(stat -c '%a' "$parent")"
    [[ "$uid" == "0" ]] || die "Refusing $parent because root does not own it."
    if nonroot_can_write_mode "$mode"; then
      die "Refusing $parent because someone other than root can replace files there."
    fi
    parent="$(dirname "$parent")"
  done
}

resolve_root() {
  local source_dir
  source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$source_dir/.release/current" ]]; then
    APP_ROOT="$source_dir"
  elif [[ -n "${PARTNERCONSOLE_ROOT:-}" ]]; then
    APP_ROOT="${PARTNERCONSOLE_ROOT%/}"
  else
    APP_ROOT="$DEFAULT_ROOT"
  fi
  APP_HOME="$APP_ROOT/.home"
}

validate_root() {
  [[ "$APP_ROOT" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "The install path must be an absolute path made of letters, numbers, dots, underscores, and slashes."
  [[ "$APP_ROOT" != *"//"* && "$APP_ROOT" != *"/."* && "$APP_ROOT" != *".."* ]] || die "The install path is not a plain directory."
  case "$APP_ROOT" in
    /|/usr|/usr/*|/etc|/etc/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*|/boot|/boot/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/root|/root/*|/tmp|/tmp/*|/var|/var/*|/home|/home/*)
      die "Choose an application directory, not a system directory."
      ;;
  esac
}

apt_install() {
  apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold install "$@"
}

verify_sha256() {
  local sum
  sum="$(sha256sum < "$1")" || return 1
  [[ "${sum%% *}" == "${2,,}" ]]
}

node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major="$(node -p "Number(process.versions.node.split('.')[0])" 2>/dev/null || echo 0)"
  [[ "$major" -ge 22 ]]
}

python_ok() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

mysql_version_ok() {
  command -v mysqld >/dev/null 2>&1 || return 1
  local version
  version="$(mysqld --version 2>/dev/null || true)"
  [[ "$version" == *" 8.4."* && "$version" != *[Mm]aria[Dd][Bb]* ]]
}

mysql_listener_is_safe() {
  [[ "$1" == "127.0.0.1:3306" || "$1" == "[::1]:3306" ]]
}

mysql_collect_db_listeners() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -lnt | awk '{
    for (i = 1; i <= NF; i++) if ($i ~ /:(3306|33060)$/) print $i
  }'
}

mysql_config_modes_ok() {
  local file_mode="$1" file_uid="$2" mode uid other
  shift 2
  [[ "$file_uid" == "0" && "$file_mode" == "644" ]] || return 1
  while [[ $# -ge 2 ]]; do
    mode="$1"
    uid="$2"
    shift 2
    [[ "$uid" == "0" ]] || return 1
    other=$((8#$mode % 8))
    [[ $((other & 1)) -eq 1 && $((other & 2)) -eq 0 ]] || return 1
  done
  [[ $# -eq 0 ]]
}

mysql_guard_nft() {
  cat <<'EOF'
table inet partnerconsole {
	chain input {
		type filter hook input priority -50; policy accept;
		tcp dport { 3306, 33060 } iifname "lo" accept
		tcp dport { 3306, 33060 } drop
	}
}
EOF
}

mysql_guard_preserves_ssh() {
  cmp -s "$1" <(mysql_guard_nft)
}

wait_mysql() {
  local _
  for _ in $(seq 1 30); do
    if mysql --protocol=socket -N -e "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_web() {
  local _
  PP_ROOT="$APP_ROOT"
  # shellcheck disable=SC1091
  source "$APP_ROOT/scripts/common.sh"
  for _ in $(seq 1 30); do
    if pp_http_matches "http://127.0.0.1:3741/" web; then
      return 0
    fi
    sleep 1
  done
  return 1
}

prepare_control_dirs() {
  local dir
  for dir in \
    "$APP_ROOT/.release" \
    "$APP_ROOT/.release/tmp" \
    "$APP_ROOT/.release/previous" \
    "$APP_ROOT/.release/incoming"
  do
    [[ ! -L "$dir" ]] || die "Refusing a symlink in the release directories."
    if [[ "$(id -u)" -eq 0 && -e "$dir" ]]; then assert_root_file "$dir"; fi
    mkdir -p "$dir"
    if [[ "$(id -u)" -eq 0 ]]; then
      chown root:root "$dir"
      chmod 700 "$dir"
    fi
  done
}

download_release_file() {
  curl --disable --fail --silent --show-error --location \
    --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 120 --retry 2 "$@"
}

assert_package_identity() {
  [[ "$RELEASE_TAG" =~ ^v(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){2}$ &&
     "$RELEASE_SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ &&
     "$RELEASE_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
    die "Use partnerconsole.sh from a published Partner Console release."
}
check_local_archive() {
  local path="$1" source="$2"
  [[ -f "$path" && -r "$path" ]] || die "Local archive is not a readable regular file: $source"
  verify_sha256 "$path" "$RELEASE_ARCHIVE_SHA256" ||
    die "Local archive checksum mismatch for $RELEASE_TAG: $source. Use the archive that matches this installer."
}

fetch_release_package() (
  local work installer="$1" archive="${2:-}"
  assert_package_identity
  prepare_control_dirs || return 1
  work="$(mktemp -d -- "$APP_ROOT/.release/package.XXXXXXXX")" || return 1
  trap 'rm -rf -- "$work"' EXIT
  if [[ -n "$archive" ]]; then
    [[ -f "$archive" && -r "$archive" ]] || die "Local archive is not a readable regular file: $archive"
    cp -- "$archive" "$work/package.tar.gz" || die "Could not copy the local archive: $archive"
    check_local_archive "$work/package.tar.gz" "$archive"
  else
    download_release_file -o "$work/package.tar.gz" \
      "$OFFICIAL_RELEASES/download/$RELEASE_TAG/partnerconsole-$RELEASE_TAG.tar.gz" ||
      die "Could not download the $RELEASE_TAG application package."
    verify_sha256 "$work/package.tar.gz" "$RELEASE_ARCHIVE_SHA256" ||
      die "Application package checksum mismatch for $RELEASE_TAG."
  fi
  mkdir "$work/unpacked" || return 1
  pp_python - "$work/package.tar.gz" "$work/unpacked" "$RELEASE_TAG" "$RELEASE_SOURCE_COMMIT" <<'PY' || return 1
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path

archive, dest, tag, commit = sys.argv[1:]
root = Path(dest)
blocked = {"dev", "tests", "data", "logs", "runtime", "backups", "node_modules", "partnerconsole.sh"}

with tarfile.open(archive, "r:gz") as package:
    members = {}
    for member in package:
        name = member.name
        parts = name.split("/")
        if (not member.isfile() or name in members or any(
                not re.fullmatch(r"[A-Za-z0-9_\[][A-Za-z0-9_.\[\]-]*", part)
                for part in parts)
                or any(part in blocked for part in parts)
                or name.startswith("deploy/generated/")
                or name in {"config/api.env", "config/web.env"}):
            sys.exit(f"Unsafe release archive member: {name!r}")
        members[name] = member
    required = {"PACKAGE.json", "backend/pyproject.toml", "package.json", "package-lock.json",
                "install.sh", "start.sh", "stop.sh", "scripts/common.sh", "scripts/backup.sh",
                "scripts/domain.sh", "scripts/render-host-templates.sh", "deploy/requirements.lock.txt",
                "config/api.env.example", "config/web.env.example",
                "deploy/nginx/partnerconsole.conf", "deploy/systemd/partnerconsole-api.service",
                "deploy/systemd/partnerconsole-web.service"}
    if not required.issubset(members):
        sys.exit("The release archive is missing required files.")
    if json.load(package.extractfile(members.pop("PACKAGE.json"))) != {"tag": tag, "commit": commit}:
        sys.exit("The release archive version or source commit does not match the installer.")
    version = f'version = "{tag[1:]}"'
    if version not in package.extractfile(members["backend/pyproject.toml"]).read().decode().splitlines():
        sys.exit("The backend package version does not match the installer.")
    for name in ("package.json", "package-lock.json"):
        if json.load(package.extractfile(members[name])).get("version") != tag[1:]:
            sys.exit(f"The version in {name} does not match the installer.")
    for name, member in members.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with package.extractfile(member) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o700 if member.mode & 0o100 else 0o600)
PY
  install -m 700 -- "$installer" "$work/unpacked/partnerconsole.sh" || return 1
  rm -rf -- "$APP_ROOT/.release/incoming" || return 1
  mv -- "$work/unpacked" "$APP_ROOT/.release/incoming"
)
sync_release() {
  local mode="$1" source="$2" dest="$3"
  pp_python - "$mode" "$source" "$dest" <<'PY'
import hashlib, os, shutil, sys, tempfile
from pathlib import Path
mode, source, dest = sys.argv[1:]
source, dest = Path(source).resolve(), Path(dest).resolve()
skip_dirs = {"data", "logs", "runtime", "backups", ".release", ".home", ".git", "node_modules", ".next"}
skip_files = {"config/api.env", "config/web.env"}

def skipped(rel: str) -> bool:
    if rel in skip_files or rel.startswith("../") or "/../" in f"/{rel}/":
        return True
    if rel.split("/", 1)[0] in skip_dirs:
        return True
    return rel == "backend/.venv" or rel.startswith("backend/.venv/")

def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def files_under(root: Path):
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        kept = []
        for name in dirnames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not skipped(rel):
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not skipped(rel):
                found.append(rel)
    return sorted(found)

def read_manifest(path: Path):
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

def write_manifest(path: Path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{item}\n" for item in items), encoding="utf-8")

def safe_unlink(root: Path, rel: str):
    if skipped(rel):
        raise SystemExit(f"Refusing to delete protected file {rel}")
    target = root / rel
    if not inside(target, root):
        raise SystemExit("Refusing to delete a path outside the install")
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        raise SystemExit(f"Refusing to delete non-file {rel}")

def copy_file(rel: str, src_root: Path, dst_root: Path):
    src = src_root / rel
    dst = dst_root / rel
    if skipped(rel) or not inside(src, src_root) or not inside(dst, dst_root):
        raise SystemExit(f"Release path escapes the tree: {rel}")
    if src.is_symlink():
        link = src.resolve()
        if not inside(link, src_root):
            raise SystemExit(f"Release symlink escapes the tree: {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src.readlink())
        return
    if not src.is_file():
        raise SystemExit(f"Release entry is not a file: {rel}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # The old installer may still be reading this file when Update returns.
    # Keep its open inode intact until it finishes.
    fd, filename = tempfile.mkstemp(prefix=".pc-release-", dir=dst.parent)
    os.close(fd)
    temp = Path(filename)
    try:
        shutil.copy2(src, temp)
        temp.replace(dst)
    finally:
        temp.unlink(missing_ok=True)

manifest = dest / ".release" / "managed.txt"
if mode == "snapshot":
    items = read_manifest(source / ".release" / "managed.txt")
    previous = dest
    if previous.exists():
        shutil.rmtree(previous)
    previous.mkdir(parents=True)
    for rel in items:
        copy_file(rel, source, previous)
    write_manifest(previous / "MANIFEST", items)
elif mode == "apply":
    items = files_under(source)
    old = set(read_manifest(manifest))
    new = set(items)
    obsolete = old - new
    fingerprints = {}
    if obsolete and (dest / ".release/current").is_file():
        fingerprints = dict(line.split("\t", 1)[::-1] for line in
                            (dest / ".release/applied.sha256").read_text(encoding="utf-8").splitlines())
    for rel in new | old:
        target = dest / rel
        if skipped(rel) or target.is_symlink() or not inside(target, dest):
            raise SystemExit(f"Refusing unsafe install path: {rel}")
        if rel in obsolete and (dest / ".release/current").is_file():
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != fingerprints.get(rel):
                raise SystemExit(f"Refusing to remove an unverified managed file: {rel}")
        if rel not in old and target.exists():
            raise SystemExit(f"Refusing to overwrite an unmanaged file: {rel}")
    write_manifest(manifest, sorted(old | set(items)))
    for rel in sorted(old - set(items)):
        safe_unlink(dest, rel)
    for rel in items:
        copy_file(rel, source, dest)
    write_manifest(manifest, items)
elif mode == "restore":
    items = read_manifest(source / "MANIFEST")
    current = set(read_manifest(manifest))
    for rel in sorted(current - set(items)):
        safe_unlink(dest, rel)
    for rel in items:
        copy_file(rel, source, dest)
    write_manifest(manifest, items)
else:
    raise SystemExit(f"Unknown sync mode {mode}")
PY
}

snapshot_release() {
  [[ "$1" == "$APP_ROOT/.release/previous" ]] || die "Refusing to snapshot into an unexpected directory."
  sync_release snapshot "$APP_ROOT" "$APP_ROOT/.release/previous"
}

apply_incoming() {
  sync_release apply "$APP_ROOT/.release/incoming" "$APP_ROOT"
}

restore_release() {
  sync_release restore "$APP_ROOT/.release/previous" "$APP_ROOT"
}

apply_permissions() {
  local gid exec_list file
  id "$APP_USER" >/dev/null 2>&1 || die "The application user does not exist."
  gid="$(id -g "$APP_USER")"
  prepare_control_dirs
  chown root:"$gid" "$APP_ROOT"
  chmod 750 "$APP_ROOT"
  install -d -o "$APP_USER" -g "$gid" -m 700 \
    "$APP_ROOT/data" \
    "$APP_ROOT/logs" \
    "$APP_ROOT/runtime" \
    "$APP_ROOT/runtime/tmp" \
    "$APP_ROOT/runtime/cache" \
    "$APP_ROOT/runtime/cache/pip" \
    "$APP_ROOT/runtime/cache/npm" \
    "$APP_ROOT/runtime/cache/xdg" \
    "$APP_ROOT/runtime/cache/pyc" \
    "$APP_ROOT/runtime/pids" \
    "$APP_HOME"
  install -d -o root -g root -m 700 "$APP_ROOT/backups"
  if [[ -d "$APP_ROOT/config" ]]; then
    chown root:"$gid" "$APP_ROOT/config"
    chmod 750 "$APP_ROOT/config"
    for file in "$APP_ROOT"/config/*; do
      [[ -f "$file" ]] || continue
      chown root:"$gid" "$file"
      chmod 640 "$file"
    done
  fi
  exec_list="$(mktemp)"
  find "$APP_ROOT" \
    \( -path "$APP_ROOT/data" -o -path "$APP_ROOT/logs" -o -path "$APP_ROOT/runtime" \
       -o -path "$APP_ROOT/backups" -o -path "$APP_ROOT/.release" -o -path "$APP_ROOT/.home" \
       -o -path "$APP_ROOT/config" -o -path "$APP_ROOT/dev" -o -path "$APP_ROOT/.github" \
       -o -path "$APP_ROOT/deploy/generated" -o -path "$APP_ROOT/release/v1.0.0/generated" \) -prune \
    -o -type f -perm -0100 -print > "$exec_list"
  find "$APP_ROOT" \
    \( -path "$APP_ROOT/data" -o -path "$APP_ROOT/logs" -o -path "$APP_ROOT/runtime" \
       -o -path "$APP_ROOT/backups" -o -path "$APP_ROOT/.release" -o -path "$APP_ROOT/.home" \
       -o -path "$APP_ROOT/config" -o -path "$APP_ROOT/dev" -o -path "$APP_ROOT/.github" \
       -o -path "$APP_ROOT/deploy/generated" -o -path "$APP_ROOT/release/v1.0.0/generated" \) -prune \
    -o -exec chown -h root:"$gid" {} +
  find "$APP_ROOT" \
    \( -path "$APP_ROOT/data" -o -path "$APP_ROOT/logs" -o -path "$APP_ROOT/runtime" \
       -o -path "$APP_ROOT/backups" -o -path "$APP_ROOT/.release" -o -path "$APP_ROOT/.home" \
       -o -path "$APP_ROOT/config" -o -path "$APP_ROOT/dev" -o -path "$APP_ROOT/.github" \
       -o -path "$APP_ROOT/deploy/generated" -o -path "$APP_ROOT/release/v1.0.0/generated" \) -prune \
    -o -type d -exec chmod 750 {} +
  find "$APP_ROOT" \
    \( -path "$APP_ROOT/data" -o -path "$APP_ROOT/logs" -o -path "$APP_ROOT/runtime" \
       -o -path "$APP_ROOT/backups" -o -path "$APP_ROOT/.release" -o -path "$APP_ROOT/.home" \
       -o -path "$APP_ROOT/config" -o -path "$APP_ROOT/dev" -o -path "$APP_ROOT/.github" \
       -o -path "$APP_ROOT/deploy/generated" -o -path "$APP_ROOT/release/v1.0.0/generated" \) -prune \
    -o -type f -exec chmod 640 {} +
  while IFS= read -r file; do
    [[ -n "$file" && -f "$file" ]] || continue
    chmod 750 "$file"
  done < "$exec_list"
  rm -f "$exec_list"
  find "$APP_ROOT" \
    \( -path "$APP_ROOT/data" -o -path "$APP_ROOT/logs" -o -path "$APP_ROOT/runtime" \
       -o -path "$APP_ROOT/backups" -o -path "$APP_ROOT/.release" -o -path "$APP_ROOT/.home" \
       -o -path "$APP_ROOT/dev" -o -path "$APP_ROOT/.github" \
       -o -path "$APP_ROOT/deploy/generated" -o -path "$APP_ROOT/release/v1.0.0/generated" \) -prune \
    -o -type f -name '*.sh' -exec chmod 750 {} +
  if [[ -d "$APP_ROOT/.next" ]]; then
    install -d -o "$APP_USER" -g "$gid" -m 700 "$APP_ROOT/.next/cache"
  fi
}

assert_runtime_locked() {
  assert_root_file "$APP_ROOT"
  assert_root_file "$APP_ROOT/partnerconsole.sh"
  assert_root_file "$APP_ROOT/install.sh"
  assert_root_file "$APP_ROOT/start.sh"
  assert_root_file "$APP_ROOT/stop.sh"
  assert_root_file "$APP_ROOT/restart.sh"
  assert_root_file "$APP_ROOT/scripts/common.sh"
  assert_root_file "$APP_ROOT/scripts/backup.sh"
  assert_root_file "$APP_ROOT/scripts/bootstrap.sh"
}

run_root_script() {
  assert_root_file "$1"
  bash "$1" "${@:2}"
}

run_app() {
  local script="$APP_ROOT/$1"
  shift
  assert_root_file "$script"
  runuser -u "$APP_USER" -- bash "$script" "$@"
}

install_node() {
  local tmp archive
  tmp="$(mktemp -d)"
  chmod 700 "$tmp"
  archive="$tmp/node.tar.xz"
  curl -fsSL -o "$archive" "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"
  verify_sha256 "$archive" "$NODE_SHA256" || die "Node.js ${NODE_VERSION} failed its checksum."
  tar -xJf "$archive" -C /usr/local --strip-components=1
  rm -rf "$tmp"
  hash -r
}

assert_mysql_config_access() {
  local path="/etc/mysql/mysql.conf.d/partnerconsole-local.cnf" mode uid dir
  local -a modes=()
  [[ -f "$path" ]] || return 1
  mode="$(stat -c '%a' "$path")"
  uid="$(stat -c '%u' "$path")"
  modes=("$mode" "$uid")
  dir="$(dirname "$path")"
  while [[ "$dir" != "/" ]]; do
    mode="$(stat -c '%a' "$dir")"
    uid="$(stat -c '%u' "$dir")"
    modes+=("$mode" "$uid")
    dir="$(dirname "$dir")"
  done
  mysql_config_modes_ok "${modes[@]}" || return 1
  if id mysql >/dev/null 2>&1; then
    runuser -u mysql -- test -r "$path" || return 1
    runuser -u mysql -- test -x /etc/mysql || return 1
    runuser -u mysql -- test -x /etc/mysql/mysql.conf.d || return 1
  fi
}

write_mysql_local_config() {
  local tmp
  install -d -o root -g root -m 755 /etc/mysql
  install -d -o root -g root -m 755 /etc/mysql/mysql.conf.d
  chmod 755 /etc/mysql /etc/mysql/mysql.conf.d
  tmp="$(mktemp)"
  cat > "$tmp" <<'EOF'
[mysqld]
bind-address = 127.0.0.1
mysqlx = 0
EOF
  install -o root -g root -m 644 "$tmp" /etc/mysql/mysql.conf.d/partnerconsole-local.cnf
  rm -f "$tmp"
  chmod 644 /etc/mysql/mysql.conf.d/partnerconsole-local.cnf
  assert_mysql_config_access
}

# Snapshot kernel listeners and socket inodes. Socket-activated listeners can
# belong to systemd alone until it passes the open sockets to sshd.
ssh_listener_snapshot() {
  pp_python - <<'PYSSH'
import os
import re
import subprocess
import sys

def endpoint(value):
    host, port = value.rsplit(":", 1)
    host = host.strip("[]")
    return ("*" if host in ("*", "::", "0.0.0.0") else host, int(port))

def is_sshd(pid):
    try:
        return os.stat(f"/proc/{pid}").st_uid == 0 and os.path.samefile(f"/proc/{pid}/exe", "/usr/sbin/sshd")
    except OSError:
        return False

socket_endpoints = set()
if subprocess.run(["systemctl", "is-active", "--quiet", "ssh.socket"]).returncode == 0:
    listen = subprocess.check_output(["systemctl", "show", "ssh.socket", "--property=Listen", "--value"], text=True)
    socket_endpoints = {endpoint(value) for value in re.findall(r"(\S+) \(Stream\)", listen)}
    if not socket_endpoints:
        sys.exit("Active ssh.socket did not report a TCP stream address.")

matched = set()
listeners = set()
for family in ("4", "6"):
    result = subprocess.check_output(["ss", "-H", f"-{family}", "-lntpe"], text=True)
    for row in result.splitlines():
        fields = row.split()
        if len(fields) < 5 or fields[0] != "LISTEN":
            continue
        owners = re.findall(r'"([^"]+)",pid=(\d+),fd=\d+', row)
        if not owners:
            continue
        local = endpoint(fields[3])
        managed = ("systemd", "1") in owners and local in socket_endpoints
        if managed:
            matched.add(local)
        if managed or any(name == "sshd" and is_sshd(pid) for name, pid in owners):
            identifier = re.search(r"\bino:(\d+)\b", row)
            if not identifier or identifier[1] == "0":
                sys.exit("Could not identify a live SSH listener socket.")
            listeners.add((family, fields[3], identifier[1]))
if socket_endpoints - matched:
    sys.exit("Active ssh.socket addresses could not be verified in the live listener list.")
if not listeners:
    sys.exit("No live SSH listener owned by sshd or active ssh.socket was found.")
for family, local, inode in sorted(listeners):
    print(f"{family} {local} {inode}")
PYSSH
}

mysql_network_guard_up() {
  local before after listener local_address tmp rules
  if ! before="$(ssh_listener_snapshot)"; then
    log "SSH listener check failed before applying the MySQL firewall guard."
    return 1
  fi
  while read -r _ local_address _; do
    case "${local_address##*:}" in
      3306|33060)
        log "SSH is listening on a port reserved for MySQL. The firewall guard was not changed."
        return 1
        ;;
    esac
  done <<< "$before"
  if ! command -v nft >/dev/null 2>&1; then
    apt_install nftables || { log "Could not install nftables for the MySQL firewall guard."; return 1; }
    hash -r
  fi
  if ! command -v nft >/dev/null 2>&1 ||
     ! install -d -o root -g root -m 755 /etc/partnerconsole ||
     ! tmp="$(mktemp)"; then
    log "Could not prepare the MySQL firewall guard."
    return 1
  fi
  if ! mysql_guard_nft > "$tmp" ||
     ! mysql_guard_preserves_ssh "$tmp" ||
     ! install -o root -g root -m 644 "$tmp" /etc/partnerconsole/mysql-guard.nft; then
    rm -f "$tmp"
    log "Could not prepare the MySQL firewall guard."
    return 1
  fi
  rm -f "$tmp"
  # Replace only this table, in one transaction so a failure keeps any old guard.
  if ! {
    if nft list table inet partnerconsole >/dev/null 2>&1; then
      printf '%s\n' 'delete table inet partnerconsole'
    fi
    cat /etc/partnerconsole/mysql-guard.nft
  } | nft -f -; then
    log "Could not apply the MySQL firewall guard for ports 3306 and 33060."
    return 1
  fi
  if ! rules="$(nft -nn list table inet partnerconsole)" ||
     ! grep -Fq 'type filter hook input priority' <<< "$rules" ||
     ! grep -Fq 'policy accept;' <<< "$rules" ||
     ! grep -Eq 'iifname "lo".*tcp dport \{ 3306, 33060 \} accept|tcp dport \{ 3306, 33060 \} iifname "lo" accept' <<< "$rules" ||
     ! grep -Fq 'tcp dport { 3306, 33060 } drop' <<< "$rules"; then
    log "Could not verify the active MySQL firewall guard for ports 3306 and 33060."
    return 1
  fi
  if ! after="$(ssh_listener_snapshot)"; then
    log "SSH listener check failed after applying the MySQL firewall guard."
    return 1
  fi
  while IFS= read -r listener; do
    if ! grep -Fxq -- "$listener" <<< "$after"; then
      log "An SSH listener changed while the MySQL firewall guard was applied."
      return 1
    fi
  done <<< "$before"
}

mysql_runtime_is_safe() {
  local listeners line saw_v4=0 bind active
  listeners="$(mysql_collect_db_listeners)" || return 1
  [[ -n "$listeners" ]] || return 1
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    mysql_listener_is_safe "$line" || return 1
    [[ "$line" == "127.0.0.1:3306" ]] && saw_v4=1
  done <<<"$listeners"
  [[ "$saw_v4" == "1" ]] || return 1
  bind="$(mysql --protocol=socket -N -e "SHOW VARIABLES LIKE 'bind_address'" | awk '{print $2}')" || return 1
  [[ "$bind" == "127.0.0.1" ]] || return 1
  active="$(mysql --protocol=socket -N -e "SELECT COUNT(*) FROM information_schema.plugins WHERE PLUGIN_NAME='mysqlx' AND PLUGIN_STATUS='ACTIVE'")" || return 1
  [[ "$active" == "0" ]]
}

mysql_fail_closed() {
  local message="$1"
  mysql_network_guard_up || true
  systemctl stop mysql >/dev/null 2>&1 || true
  die "$message"
}

install_mysql_packages() {
  local tmp deb
  tmp="$(mktemp -d)"
  chmod 700 "$tmp"
  deb="$tmp/mysql-apt-config.deb"
  curl -fsSL -o "$deb" "$MYSQL_DEB_URL"
  verify_sha256 "$deb" "$MYSQL_DEB_SHA256" || return 1
  echo "mysql-apt-config mysql-apt-config/select-server select mysql-8.4-lts" | debconf-set-selections
  DEBIAN_FRONTEND=noninteractive dpkg -i "$deb" || return 1
  rm -rf "$tmp"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt_install mysql-server
}

ensure_mysql() {
  if command -v mysqld >/dev/null 2>&1 && ! mysql_version_ok; then
    die "MySQL is already installed, but it is not 8.4. It was left unchanged."
  fi
  mysql_network_guard_up || die "MySQL preflight failed. Installation or Update stopped before deployment."
  write_mysql_local_config || mysql_fail_closed "The MySQL configuration was not readable before startup. Installation stopped."
  if ! mysql_version_ok; then
    log "Installing MySQL 8.4..."
    install_mysql_packages || mysql_fail_closed "MySQL installation failed. Public database ports stay blocked, and SSH was not changed."
  fi
  assert_mysql_config_access || mysql_fail_closed "MySQL cannot read its local configuration. Installation stopped."
  systemctl enable mysql >/dev/null 2>&1 || true
  if ! mysql_runtime_is_safe; then
    systemctl restart mysql || mysql_fail_closed "MySQL did not restart under the local configuration."
    wait_mysql || mysql_fail_closed "MySQL did not accept local connections."
    mysql_runtime_is_safe || mysql_fail_closed "MySQL is exposed or its effective configuration is not local. Public access stays blocked."
  fi
}

install_deps() {
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  [[ -f /etc/os-release ]] || die "This installer supports Ubuntu only."
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || die "Ubuntu 24.04 is required. This server is ${PRETTY_NAME:-unknown}."
  log "Installing base packages..."
  # nginx and certbot are not installed here. Domain & SSL asks before installing them.
  apt-get update
  apt_install ca-certificates curl gnupg python3 python3-venv python3-pip build-essential pkg-config lsb-release xz-utils iproute2
  python_ok || die "Python 3.12 was not found."
  if ! node_ok; then
    log "Installing Node.js ${NODE_VERSION}..."
    install_node
  fi
  node_ok || die "Node.js 22 was not installed."
  ensure_mysql
  mysql_version_ok || die "MySQL 8.4 is not installed. Found: $(mysqld --version 2>/dev/null || echo missing)"
  log "Requirements are ready: Python $(python3 -V | awk '{print $2}'), Node $(node -v), MySQL 8.4."
}

ensure_install_directory() {
  if [[ ! -d "$APP_ROOT" ]]; then
    install -d -o root -g root -m 750 "$APP_ROOT"
  fi
}

ensure_user() {
  install -d -o root -g root -m 755 "$(dirname "$APP_ROOT")"
  ensure_install_directory
  if ! id "$APP_USER" >/dev/null 2>&1; then
    install -d -o root -g root -m 755 "$APP_HOME"
    useradd --system --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
  fi
  prepare_control_dirs
}

install_launcher() {
  local launcher="/usr/local/bin/partnerconsole"
  cat > "$launcher" <<EOF
#!/bin/bash
set -euo pipefail
target="$APP_ROOT/partnerconsole.sh"
uid="\$(stat -c '%u' "\$target")"
mode="\$(stat -c '%a' "\$target")"
if [[ "\$uid" != "0" || \$((8#\$mode & 022)) -ne 0 ]]; then
  printf '%s\n' "Refusing to run a Partner Console command that root does not exclusively control." >&2
  exit 1
fi
exec /usr/bin/bash "\$target" "\$@"
EOF
  chown root:root "$launcher"
  chmod 755 "$launcher"
}

write_marker() {
  local commit="$1" marker
  marker="$(mktemp -- "$APP_ROOT/.release/current.XXXXXXXX")" || return 1
  if ! printf 'tag=%s\ncommit=%s\narchive=%s\n' "$RELEASE_TAG" "$commit" "$RELEASE_ARCHIVE_SHA256" > "$marker" ||
      ! mv -f -- "$marker" "$APP_ROOT/.release/current"; then
    rm -f -- "$marker"
    return 1
  fi
}

current_commit() {
  local line
  [[ -f "$APP_ROOT/.release/current" ]] || return 1
  line="$(sed -n 's/^commit=//p' "$APP_ROOT/.release/current")"
  [[ "$line" =~ ^[0-9a-f]{40}$ ]] || return 1
  printf '%s\n' "$line"
}

record_applied_files() {
  [[ -f "$APP_ROOT/.release/managed.txt" ]] || return 1
  pp_python - "$APP_ROOT" <<'PY' || return 1
import hashlib
import sys
from pathlib import Path
root = Path(sys.argv[1])
managed = root / ".release" / "managed.txt"
target = root / ".release" / "applied.sha256"
lines = []
for rel in managed.read_text(encoding="utf-8").splitlines():
    if not rel or rel.startswith("../") or "/../" in f"/{rel}/":
        continue
    path = root / rel
    if path.is_symlink() or not path.is_file():
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lines.append(f"{digest}\t{rel}")
target.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
PY
  chmod 600 "$APP_ROOT/.release/applied.sha256" 2>/dev/null || true
}

release_differences() {
  pp_python - "$1" "$APP_ROOT" "$APP_ROOT/.release/incoming" <<'PY'
import hashlib
import sys
from pathlib import Path
mode, root_text, incoming_text = sys.argv[1:]
root = Path(root_text)
incoming = Path(incoming_text)
skip_dirs = {"data", "logs", "runtime", "backups", ".release", ".home", ".git", "node_modules", ".next"}
skip_files = {"config/api.env", "config/web.env"}

def skipped(rel: str) -> bool:
    if not rel or rel in skip_files or rel.startswith("../") or "/../" in f"/{rel}/":
        return True
    if rel.split("/", 1)[0] in skip_dirs:
        return True
    return rel == "backend/.venv" or rel.startswith("backend/.venv/")

def digest(path: Path):
    if path.is_symlink() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()

changed = []
if mode == "recorded":
    record = root / ".release" / "applied.sha256"
    for line in record.read_text(encoding="utf-8").splitlines():
        if "\t" not in line:
            continue
        expected, rel = line.split("\t", 1)
        if skipped(rel):
            continue
        actual = digest(root / rel)
        if actual != expected:
            changed.append(rel)
elif mode == "incoming":
    managed = root / ".release" / "managed.txt"
    paths = []
    if managed.is_file():
        paths.extend(managed.read_text(encoding="utf-8").splitlines())
    if incoming.is_dir():
        for path in incoming.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(incoming).as_posix()
            if rel not in paths:
                paths.append(rel)
    for rel in paths:
        if skipped(rel):
            continue
        current = root / rel
        if not current.exists():
            continue
        if digest(current) != digest(incoming / rel):
            changed.append(rel)
for rel in sorted(set(changed)):
    print(rel)
PY
}

reject_local_changes() {
  local previous="$1" new_commit="$2" changes
  if [[ -f "$APP_ROOT/.release/applied.sha256" ]]; then
    changes="$(release_differences recorded)"
  elif [[ -z "$previous" ]]; then
    return 0
  elif [[ "$previous" != "$new_commit" ]]; then
    die "Update stopped. This install has no file fingerprints, so local changes cannot be separated from the new release. No file was changed."
  else
    changes="$(release_differences incoming)"
  fi
  if [[ -n "${changes:-}" ]]; then
    log "Update stopped. These installed files differ from the recorded release:"
    printf '%s\n' "$changes" >&2
    die "No file was changed. Configuration and the database were not changed."
  fi
}

assert_next_module() {
  local cookie="$APP_ROOT/node_modules/next/dist/compiled/cookie"
  [[ -d "$APP_ROOT/node_modules/next" ]] || return 0
  if [[ ! -f "$cookie/index.js" || ! -f "$cookie/package.json" ]]; then
    log "Next.js installed without next/dist/compiled/cookie."
    return 1
  fi
  if ! (
    cd "$APP_ROOT" || exit 1
    runuser -u "$APP_USER" -- node -e "require('next/dist/compiled/cookie')"
  ); then
    log "The application user cannot load Next.js."
    return 1
  fi
}

rollback_failed_release() {
  restore_release || return 2
  rm -rf -- "$APP_ROOT/node_modules" "$APP_ROOT/.next" "$APP_ROOT/backend/.venv"
  apply_permissions || true
  run_root_script "$APP_ROOT/install.sh" || return 1
  apply_permissions || true
  assert_next_module || return 1
}

deploy_fetched() {
  local commit="$1" previous failed
  previous="$(current_commit || true)"
  reject_local_changes "$previous" "$commit"
  snapshot_release "$APP_ROOT/.release/previous" || die "Could not record the current code. No file was changed. The database was not changed."
  failed=0
  apply_incoming || failed=1
  if [[ "$failed" -eq 0 ]]; then
    apply_permissions || failed=1
  fi
  if [[ "$failed" -eq 0 ]]; then
    run_root_script "$APP_ROOT/install.sh" || failed=1
  fi
  if [[ "$failed" -eq 0 ]]; then
    apply_permissions || failed=1
  fi
  if [[ "$failed" -eq 0 ]]; then
    assert_next_module || failed=1
  fi
  if [[ "$failed" -ne 0 ]]; then
    if rollback_failed_release; then
      die "The code update failed. The previous code was restored. The database was not rolled back."
    elif [[ "$?" -eq 2 ]]; then
      die "The code update failed and the previous code could not be restored. The database was not rolled back."
    else
      die "The code update failed. The previous code and directory access were restored, but dependencies could not be rebuilt. The database was not rolled back."
    fi
  fi
  # A visible release marker must identify files that are ready to resume.
  record_applied_files || die "Could not record the installed file fingerprints."
  write_marker "$commit" || die "Could not record the installed release."
}

release_already_current() {
  local commit="$1" previous differences
  previous="$(current_commit || true)"
  [[ -n "$previous" && "$previous" == "$commit" ]] || return 1
  grep -Fxq "tag=$RELEASE_TAG" "$APP_ROOT/.release/current" || return 1
  grep -Fxq "archive=$RELEASE_ARCHIVE_SHA256" "$APP_ROOT/.release/current" || return 1
  [[ -f "$APP_ROOT/.release/applied.sha256" ]] || return 1
  differences="$(release_differences recorded)" || return 1
  [[ -z "$differences" ]]
}

ask_origin() {
  local origin web_base
  while true; do
    log "Enter the site HTTPS origin, for example https://console.example.com"
    log "Press Enter if you do not have a domain yet. The app stays on this server and is not published."
    tty_read -p "HTTPS origin: " origin || return 1
    if [[ -z "$origin" ]]; then
      origin="http://127.0.0.1:3741"
      web_base="http://127.0.0.1:8741"
      log "No domain is configured. The app is not published."
      log "From your computer, open an SSH tunnel and browse to http://127.0.0.1:3741:"
      log "ssh -L 3741:127.0.0.1:3741 -L 8741:127.0.0.1:8741 user@server"
      break
    fi
    if ORIGIN="$origin" pp_python - <<'PY'
import os, sys
from urllib.parse import urlsplit
parts = urlsplit(os.environ["ORIGIN"])
ok = (
    parts.scheme == "https" and parts.hostname and not parts.username
    and not parts.password and parts.path in ("",) and not parts.query
    and not parts.fragment and "*" not in os.environ["ORIGIN"]
    and "panel.example.com" not in os.environ["ORIGIN"]
    and "REPLACE_WITH" not in os.environ["ORIGIN"]
)
sys.exit(0 if ok else 1)
PY
    then
      web_base="$origin"
      break
    fi
    log "The origin must be an exact HTTPS URL, with no path and no placeholder."
  done
  printf '%s\n%s\n' "$origin" "$web_base"
}

ask_admin_password() {
  local first second
  while true; do
    tty_silent -p "Initial admin password (at least 12 characters, not saved): " first || return 1
    tty_silent -p "Repeat password: " second || return 1
    if [[ "$first" != "$second" ]]; then
      log "The passwords do not match."
      continue
    fi
    if [[ "${#first}" -lt 12 || "$first" == "admin12345" ]]; then
      log "Use at least 12 characters, and do not use admin12345."
      continue
    fi
    printf '%s' "$first"
    return
  done
}

assert_mysql_ident() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]{0,63}$ ]] || die "Refusing an unsafe MySQL identifier."
}

assert_database_empty() {
  local count
  assert_mysql_ident "$DB_NAME"
  wait_mysql || die "MySQL did not accept local connections."
  count="$(mysql --protocol=socket -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}'")"
  [[ "$count" == "0" ]] || die "Database ${DB_NAME} already has tables. Refusing to change it."
}

create_database() {
  local db_password="$1"
  assert_mysql_ident "$DB_NAME"
  assert_mysql_ident "$DB_USER"
  assert_database_empty
  DB_PASSWORD="$db_password" DB_USER="$DB_USER" DB_NAME="$DB_NAME" pp_python - <<'PY'
import os, re, shutil, subprocess
password = os.environ["DB_PASSWORD"]
user = os.environ["DB_USER"]
name = os.environ["DB_NAME"]
ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
if ident.fullmatch(name) is None or ident.fullmatch(user) is None:
    raise SystemExit("Refusing an unsafe MySQL identifier.")
if any(char in password for char in "\n\r\x00\\"):
    raise SystemExit("Refusing an unsafe database password.")
password = password.replace("'", "''")
quote = chr(96)
sql = (
    f"CREATE DATABASE IF NOT EXISTS {quote}{name}{quote} CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;\n"
    f"CREATE USER IF NOT EXISTS '{user}'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
    f"ALTER USER '{user}'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
    "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, LOCK TABLES\n"
    f"  ON {quote}{name}{quote}.* TO '{user}'@'127.0.0.1';\n"
    "FLUSH PRIVILEGES;\n"
)
program = shutil.which("mysql")
if program is None:
    raise SystemExit("mysql client was not found.")
subprocess.run([program, "--protocol=socket"], input=sql, text=True, check=True)
PY
}

# 0: an admin exists, 1: bootstrap is needed, 2: readiness could not be verified.
database_ready() {
  local tables admins
  assert_mysql_ident "$DB_NAME"
  tables="$(mysql --protocol=socket -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${DB_NAME}' AND table_name='admin_users'")" || return 2
  [[ "$tables" != "0" ]] || return 1
  [[ "$tables" == "1" ]] || return 2
  admins="$(mysql --protocol=socket -N -e "SELECT COUNT(*) FROM ${DB_NAME}.admin_users")" || return 2
  [[ "$admins" =~ ^[0-9]+$ ]] || return 2
  [[ "$admins" != "0" ]]
}

write_config() {
  local origin="$1" web_base="$2" db_password="$3" gid
  gid="$(id -g "$APP_USER")"
  install -d -o root -g "$gid" -m 750 "$APP_ROOT/config"
  ORIGIN="$origin" WEB_BASE="$web_base" DB_PASSWORD="$db_password" APP_ROOT="$APP_ROOT" DB_USER="$DB_USER" DB_NAME="$DB_NAME" APP_GID="$gid" pp_python - <<'PY'
import os
import secrets
from pathlib import Path
root = Path(os.environ["APP_ROOT"])
gid = int(os.environ["APP_GID"])
api = "\n".join([
    "ENVIRONMENT=production",
    f"DATABASE_URL=mysql+pymysql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}@127.0.0.1:3306/{os.environ['DB_NAME']}?charset=utf8mb4",
    f"SECRET_KEY={secrets.token_urlsafe(48)}",
    f"ADMIN_JWT_SECRET={secrets.token_urlsafe(48)}",
    f"PARTNER_JWT_SECRET={secrets.token_urlsafe(48)}",
    "PANEL_ENCRYPTION_KEY=",
    "ADMIN_USERNAME=admin",
    "ACCESS_TOKEN_EXPIRE_MINUTES=720",
    f"CORS_ORIGINS={os.environ['ORIGIN']}",
    "API_RATE_LIMIT_PER_MINUTE=120",
    "PANEL_PRIVATE_ORIGINS=",
    "",
])
web = f"NEXT_PUBLIC_API_BASE={os.environ['WEB_BASE']}\n"
for name, text in (("api.env", api), ("web.env", web)):
    path = root / "config" / name
    path.write_text(text, encoding="utf-8", newline="\n")
    os.chown(path, 0, gid)
    os.chmod(path, 0o640)
PY
}

setup_app() (
  local install_lock ready=0 archive="${1:-}"
  assert_package_identity
  [[ ! -L "$APP_ROOT" ]] || die "Refusing a symbolic link at the install path."
  ensure_install_directory
  assert_root_file "$APP_ROOT"
  exec {install_lock}< "$APP_ROOT" || die "Cannot open the install directory."
  flock -n "$install_lock" || die "Another Install or Update is running for this installation."
  if [[ -e "$APP_ROOT/.release/current" ]]; then
    assert_root_file "$APP_ROOT/.release/current"
    release_already_current "$RELEASE_SOURCE_COMMIT" ||
      die "This installation differs from the downloaded release. Use Update for a different version or review local file changes."
    assert_runtime_locked
    log "The existing release and configuration were kept."
  else
    install_deps
    ensure_user
    fetch_release_package "${BASH_SOURCE[0]}" "$archive" || return 1
    if [[ -f "$APP_ROOT/config/api.env" ]]; then
      log "The existing configuration was kept."
    else
      local origin web_base db_password answers
      local -a origin_lines
      answers="$(ask_origin)" || return 1
      mapfile -t origin_lines <<< "$answers"
      origin="${origin_lines[0]}"
      web_base="${origin_lines[1]}"
      db_password="$(pp_python -c 'import secrets; print(secrets.token_urlsafe(24))')"
      create_database "$db_password"
      write_config "$origin" "$web_base" "$db_password"
      unset db_password
      log "The database password is only in config/api.env and was not printed."
    fi
    deploy_fetched "$RELEASE_SOURCE_COMMIT"
  fi
  assert_root_file "$APP_ROOT/config/api.env"
  assert_root_file "$APP_ROOT/config/web.env"
  install_launcher
  database_ready || ready=$?
  case "$ready" in
    0) log "The database is already initialized. Bootstrap was not run again." ;;
    1)
      local admin_password
      assert_root_file "$APP_ROOT/scripts/bootstrap.sh"
      if [[ ! -d "$APP_ROOT/deploy/generated" ]]; then
        PP_RENDER_ONLY=1 PP_ROOT="$APP_ROOT" run_root_script "$APP_ROOT/scripts/render-host-templates.sh" "$APP_USER" || return 1
      fi
      admin_password="$(ask_admin_password)" || return 1
      builtin printf '%s\n' "$admin_password" |
        runuser -u "$APP_USER" -- bash "$APP_ROOT/scripts/bootstrap.sh" --confirm-database --password-stdin
      unset admin_password
      ;;
    *) die "Could not check administrator initialization. Check the local MySQL service and rerun Install." ;;
  esac
  log "Install finished. Do not expose ports 8741, 3741, or 3306."
  log "Start: sudo partnerconsole start"
)

update_app() {
  [[ -f "$APP_ROOT/config/api.env" ]] || die "Install Partner Console before updating it."
  local commit="$RELEASE_SOURCE_COMMIT" service_state=0 archive="${1:-}"
  if [[ -f "$APP_ROOT/.release/current" ]] && grep -Fxq "tag=$RELEASE_TAG" "$APP_ROOT/.release/current"; then
    reject_local_changes "$(current_commit || true)" "$commit"
    release_already_current "$commit" ||
      die "Release $RELEASE_TAG is already installed, but its saved identity differs. Refusing to reinstall the same version."
    log "This install is already release ${RELEASE_TAG}. No file was changed."
    return 0
  fi
  reject_local_changes "$(current_commit || true)" "$commit"
  fetch_release_package "${BASH_SOURCE[0]}" "$archive" || return 1
  app_running "$APP_ROOT/.release/incoming/scripts/common.sh" || service_state=$?
  [[ "$service_state" -lt 2 ]] || die "Cannot verify the application processes and ports. Update stopped."
  ensure_user
  install_deps
  deploy_fetched "$commit"
  install_launcher
  if [[ "$service_state" == 0 ]]; then
    restart_app || die "The update is installed, but the application restart failed."
  fi
  log "Update finished on tag ${RELEASE_TAG}. Configuration and database data were kept."
}

installed_release_tag() {
  local marker="$APP_ROOT/.release/current" tag
  assert_root_file "$marker"
  tag="$(sed -n 's/^tag=//p' "$marker")"
  [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]] ||
    die "The installed release marker has no valid version."
  printf '%s\n' "$tag"
}

release_is_older() {
  local target="$1" installed="$2"
  # dpkg treats a hyphen as a packaging revision. A release candidate must
  # instead sort below the stable version with the same major/minor/patch.
  installed="${installed/-rc./~rc.}"
  dpkg --compare-versions "${target#v}" lt "${installed#v}"
}

apply_pinned_update() (
  local update_lock installed
  exec {update_lock}< "$APP_ROOT" || die "Cannot open the install directory."
  flock -n "$update_lock" || die "Another Update is running for this installation."
  if [[ -f "$APP_ROOT/.release/current" ]]; then
    installed="$(installed_release_tag)" || return 1
    if release_is_older "$RELEASE_TAG" "$installed"; then
      die "Release $installed is already installed. Refusing to downgrade to $RELEASE_TAG."
    fi
  fi
  # Keep the lock in this process only. Restarted services must not inherit it.
  ( update_app "$@" ) {update_lock}<&-
)

update_command() (
  [[ -f "$APP_ROOT/config/api.env" ]] || die "Install Partner Console before updating it."
  if [[ -n "${1:-}" || ! "${BASH_SOURCE[0]}" -ef "$APP_ROOT/partnerconsole.sh" ]]; then
    # An archive or a separately downloaded installer selects a pinned release.
    apply_pinned_update "$@"
    return
  fi

  local work metadata tag digest installed
  local -a download=(curl --disable --fail --silent --show-error --location
    --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 60 --retry 2)
  assert_root_file "$APP_ROOT/.release"
  installed="$(installed_release_tag)" || return 1
  work="$(mktemp -d -- "$APP_ROOT/.release/update.XXXXXXXX")" || die "Could not create a private Update directory."
  trap 'rm -rf -- "$work"' EXIT
  "${download[@]}" -o "$work/release.json" \
    'https://api.github.com/repos/MoonlessWanderer/PartnerConsole/releases/latest' ||
    die "Could not retrieve the latest Partner Console release from GitHub."
  metadata="$(pp_python - "$work/release.json" <<'PY'
import json, re, sys
from pathlib import Path

try:
    release = json.loads(Path(sys.argv[1]).read_bytes())
except (OSError, ValueError):
    sys.exit("Invalid GitHub release metadata.")
if not isinstance(release, dict):
    sys.exit("Invalid GitHub release metadata.")
tag = release.get("tag_name", "")
if (release.get("draft") is not False or release.get("prerelease") is not False
        or not isinstance(tag, str)
        or not re.fullmatch(r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}", tag)):
    sys.exit("Expected a published, stable Partner Console version.")
assets = release.get("assets")
if not isinstance(assets, list):
    sys.exit("The release must contain one uploaded partnerconsole.sh asset.")
for name in (f"partnerconsole-{tag}.tar.gz", "SHA256SUMS"):
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if (len(matches) != 1 or matches[0].get("state") != "uploaded"
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(matches[0].get("digest")))):
        sys.exit(f"The release must contain one uploaded {name} with a SHA256 digest.")
assets = [a for a in assets if isinstance(a, dict) and a.get("name") == "partnerconsole.sh"]
if len(assets) != 1 or assets[0].get("state") != "uploaded":
    sys.exit("The release must contain one uploaded partnerconsole.sh asset.")
digest = assets[0].get("digest") or ""
if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    sys.exit("GitHub did not provide a SHA256 digest for the installer.")
print(tag, digest.removeprefix("sha256:"))
PY
  )" || die "Invalid GitHub release metadata. Update stopped."
  read -r tag digest <<< "$metadata"
  if release_is_older "$tag" "$installed"; then
    die "GitHub lists $tag as latest. Refusing to downgrade from $installed."
  fi
  if [[ "$tag" == "$installed" ]]; then
    [[ "$RELEASE_TAG" == "$installed" ]] || die "The installed launcher and release marker disagree. Update stopped."
    apply_pinned_update
    return
  fi
  log "Updating Partner Console from $installed to $tag."
  "${download[@]}" -o "$work/partnerconsole.sh" \
    "$OFFICIAL_RELEASES/download/$tag/partnerconsole.sh" ||
    die "Could not download the $tag installer."
  verify_sha256 "$work/partnerconsole.sh" "$digest" || die "Installer checksum mismatch for $tag."
  [[ "$(sed -n '/^RELEASE_TAG=/p' "$work/partnerconsole.sh")" == "RELEASE_TAG=\"$tag\"" ]] ||
    die "Installer tag mismatch for $tag."
  PARTNERCONSOLE_ROOT="$APP_ROOT" /usr/bin/bash "$work/partnerconsole.sh" update
)


# 0 = running, 1 = stopped, 2 = unsafe or indeterminate.
app_running() (
  local PP_ROOT="$APP_ROOT" common="${1:-$APP_ROOT/scripts/common.sh}" uid dir api_state=0 web_state=0
  (assert_root_file "$common") || return 2
  uid="$(id -u "$APP_USER")" || return 2
  [[ "$uid" =~ ^[1-9][0-9]*$ ]] || return 2
  for dir in "$PP_ROOT/runtime" "$PP_ROOT/runtime/pids"; do
    [[ ! -L "$dir" && ( ! -e "$dir" || -d "$dir" ) ]] || return 2
  done
  [[ ! -e "$PP_ROOT/runtime/pids/lock" && ! -L "$PP_ROOT/runtime/pids/lock" ]] || return 2
  # shellcheck disable=SC1090
  source "$common" || return 2
  pp_service_state api 8741 "$uid" || api_state=$?
  pp_service_state web 3741 "$uid" || web_state=$?
  [[ "$api_state" -lt 2 && "$web_state" -lt 2 ]] || return 2
  [[ "$api_state" == 0 || "$web_state" == 0 ]]
)

start_app() {
  assert_runtime_locked
  run_app start.sh
  wait_web || die "The API started, but the web page did not return HTTP 200."
}
stop_app() {
  assert_runtime_locked
  local service_state=0
  app_running || service_state=$?
  [[ "$service_state" -lt 2 ]] || { log "Cannot verify the application processes and ports. Stop refused."; return 1; }
  run_app stop.sh
}
restart_app() (
  assert_runtime_locked
  local service_state=0
  app_running || service_state=$?
  [[ "$service_state" -lt 2 ]] || { log "Cannot verify the application processes and ports. Restart refused."; return 1; }
  run_app restart.sh || return 1
  wait_web || die "The API restarted, but the web page did not return HTTP 200."
)
status_app() {
  assert_runtime_locked
  run_app status.sh
}
logs_app() {
  if [[ -f "$APP_ROOT/logs/api.log" ]]; then
    tail -n 40 "$APP_ROOT/logs/api.log"
  else
    log "The API log does not exist yet."
  fi
  if [[ -f "$APP_ROOT/logs/web.log" ]]; then
    tail -n 40 "$APP_ROOT/logs/web.log"
  else
    log "The web log does not exist yet."
  fi
}
backup_app() {
  assert_runtime_locked
  local service_state=0
  app_running || service_state=$?
  case "$service_state" in
    0) die "Stop the app before taking a backup." ;;
    1) ;;
    *) die "Cannot verify that the app is stopped. Backup refused." ;;
  esac
  run_root_script "$APP_ROOT/scripts/backup.sh" create || return 1
  log "The backup is in $APP_ROOT/backups."
}

backup_menu() {
  local choice
  while true; do
    printf '\nBackup & Restore\n'
    printf 'A backup is a database dump, not a copy of config or encryption keys.\n'
    printf 'MySQL restore is not atomic. A failed restore can leave the database incomplete.\n\n'
    printf '  1) Create Backup\n'
    printf '  2) Restore Backup\n'
    printf '  3) List Backups\n'
    printf '  0) Back\n\n'
    tty_read -p "Choice: " choice
    case "$choice" in
      1) (set -e; run_choice backup) || log "Failed." ;;
      2) (set -e; run_root_script "$APP_ROOT/scripts/backup.sh" restore) || log "Failed." ;;
      3) (set -e; run_root_script "$APP_ROOT/scripts/backup.sh" list) || log "Failed." ;;
      0) return 0 ;;
      *) log "Invalid choice." ;;
    esac
    pause_menu
  done
}

domain_app() {
  assert_runtime_locked
  local PP_ROOT="$APP_ROOT"
  local PP_DOMAIN_LIBRARY=1
  # shellcheck disable=SC1091
  source "$APP_ROOT/scripts/domain.sh" || return 1
  pp_domain_setup
}

assert_credentials_file() {
  [[ -f "$1" && "$(realpath -e -- "$1")" == "$1" ]] || die "Missing or unsafe Credentials file: $1"
  assert_root_file "$1"
}

credentials_app() (
  local PP_ROOT="$APP_ROOT" env_file="$APP_ROOT/config/api.env" database_url mode config_lock python
  assert_runtime_locked
  assert_credentials_file "$APP_ROOT/scripts/common.sh"
  assert_credentials_file "$APP_ROOT/scripts/credentials.sh"
  assert_credentials_file "$env_file"
  mode="$(stat -c '%a' -- "$env_file")" || return 1
  [[ -r "$env_file" && $((8#$mode & 0400)) -ne 0 ]] || die "Cannot read config/api.env."
  exec {config_lock}< "${env_file%/*}" || die "Cannot open the API configuration directory."
  flock -n "$config_lock" || die "Another Credentials menu is already open for this installation."
  # shellcheck disable=SC1091
  source "$APP_ROOT/scripts/common.sh" || return 1
  # Import only the database setting. Other dotenv keys must not change the root command's environment.
  set +x
  database_url="$(
    unset DATABASE_URL
    pp_load_env_file "$env_file" || exit 1
    printf '%s' "${DATABASE_URL:-}"
  )" || return 1
  [[ -n "$database_url" ]] || die "Set DATABASE_URL in config/api.env before changing credentials."
  python="$PP_ROOT/backend/.venv/bin/python"
  [[ -x "$python" ]] || die "The installed Python environment is missing."
  assert_root_file "${python%/*}"
  [[ "$(stat -c '%u' -- "$python")" == 0 ]] || die "Root does not own the installed Python launcher."
  assert_root_file "$(realpath -e -- "$python")"
  export DATABASE_URL="$database_url"
  # shellcheck disable=SC1091
  source "$APP_ROOT/scripts/credentials.sh" || return 1
  pp_credentials_menu
)

run_choice() {
  local command="$1" archive=""
  shift
  if [[ "$command" == install || "$command" == update ]] && (( $# )); then
    [[ $# == 2 && "$1" == --from-archive && -n "${2:-}" ]] ||
      die "Usage: sudo bash ./partnerconsole.sh $command [--from-archive PATH]"
    archive="$2"
    assert_package_identity
    check_local_archive "$archive" "$archive"
  fi
  case "$command" in
    install) setup_app "$archive" ;;
    start) start_app ;;
    stop) stop_app ;;
    restart) restart_app ;;
    update) update_command "$archive" ;;
    status) status_app ;;
    logs) logs_app ;;
    backup) backup_app ;;
    domain) domain_app ;;
    credentials) credentials_app ;;
    *) die "Unknown command." ;;
  esac
}

menu_loop() {
  local choice
  while true; do
    printf '\nPartner Console\nFolder: %s\nRelease: %s\n\n' "$APP_ROOT" "$RELEASE_TAG"
    printf '  1) Install\n'
    printf '  2) Start\n'
    printf '  3) Stop\n'
    printf '  4) Restart\n'
    printf '  5) Update\n'
    printf '  6) Status\n'
    printf '  7) Logs\n'
    printf '  8) Backup & Restore\n'
    printf '  9) Domain & SSL\n'
    printf ' 10) Admin Credentials\n'
    printf '  0) Exit\n\n'
    tty_read -p "Choice: " choice
    case "$choice" in
      1)
        # Start a new Bash so the menu's failure handler cannot suppress errexit.
        PARTNERCONSOLE_ROOT="$APP_ROOT" /usr/bin/bash "${BASH_SOURCE[0]}" install || log "Failed."
        ;;
      2) (set -e; run_choice start) || log "Failed." ;;
      3) (set -e; run_choice stop) || log "Failed." ;;
      4) (set -e; run_choice restart) || log "Failed." ;;
      5)
        if PARTNERCONSOLE_ROOT="$APP_ROOT" /usr/bin/bash "${BASH_SOURCE[0]}" update; then
          if [[ "${BASH_SOURCE[0]}" -ef "$APP_ROOT/partnerconsole.sh" ]]; then
            exec /usr/bin/bash "$APP_ROOT/partnerconsole.sh" menu
          fi
        else
          log "Failed."
        fi
        ;;
      6) (set -e; run_choice status) || log "Failed." ;;
      7) (set -e; run_choice logs) || log "Failed." ;;
      8) (set -e; backup_menu) || log "Failed." ;;
      9) (set -e; run_choice domain) || log "Failed." ;;
      10) (set -e; run_choice credentials) || log "Failed." ;;
      0) exit 0 ;;
      *) log "Invalid choice." ;;
    esac
    pause_menu
  done
}

usage() {
  cat <<EOF
sudo partnerconsole
sudo partnerconsole install|start|stop|restart|update|status|logs|backup|domain|credentials
sudo bash ./partnerconsole.sh install|update --from-archive PATH
EOF
}

if [[ "${PARTNERCONSOLE_LIBRARY:-}" != "1" ]]; then
  if [[ ! -f "${BASH_SOURCE[0]:-}" ]]; then
    die "Do not pipe this script into bash. Save it, then run: sudo bash /tmp/partnerconsole.sh"
  fi
  require_root
  resolve_root
  validate_root
  if [[ "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" == "$APP_ROOT" ]]; then
    assert_root_file "${BASH_SOURCE[0]}"
  fi
  case "${1:-menu}" in
    menu) menu_loop ;;
    install|start|stop|restart|update|status|logs|backup|domain|credentials) run_choice "$@" ;;
    *) usage; exit 2 ;;
  esac
fi
