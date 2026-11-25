#!/usr/bin/env bash
# Database dump only. This is not a copy of config, encryption keys, or the
# application. MySQL restore is not atomic: a failed import can leave the
# database incomplete, and this script does not promise to undo that.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

pp_backup_root() {
  readlink -f "$PP_ROOT/backups"
}

pp_backup_inside() {
  local root resolved
  root="$(pp_backup_root)"
  [[ -f "$1" && ! -L "$1" ]] || return 1
  resolved="$(readlink -f "$1")"
  [[ "$resolved" == "$root"/* ]]
}

pp_backup_python() {
  local candidate
  if declare -F pp_python >/dev/null; then
    pp_python "$@"
    return
  fi
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
      "$candidate" "$@"
      return
    fi
  done
  echo "Python is required." >&2
  return 1
}

pp_backup_sql_rejected() {
  pp_backup_python - "$1" <<'PY'
import re
import sys
from pathlib import Path
path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file():
    raise SystemExit(0)
data = path.read_bytes()
if b"\0" in data:
    raise SystemExit(0)
text = data.decode("utf-8", "replace")
pattern = re.compile(
    r"(?im)(into\s+(outfile|dumpfile)|load\s+data|load_file\s*\(|^\s*system\s|^\s*source\s|"
    r"create\s+user|drop\s+database|create\s+database|\bgrant\s|\brevoke\s|set\s+global)"
)
# mysqldump writes extended INSERT statements as single lines. Only skip text in
# literal-only VALUES rows, leaving all other statements and comments visible.
insert = re.compile(r"^INSERT INTO `[A-Za-z0-9_]+` VALUES (.+);$")
quoted = re.compile(r"'(?:[^'\\]|\\.|'')*'")
scalar = r"(?:NULL|0x[0-9A-Fa-f]+|[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
row = rf"\({scalar}(?:,{scalar})*\)"
literal_rows = re.compile(rf"{row}(?:,{row})*")
if "NO_BACKSLASH_ESCAPES" not in text.upper():
    lines = []
    for line in text.split("\n"):
        match = insert.fullmatch(line)
        if match and literal_rows.fullmatch(quoted.sub("NULL", match[1])):
            lines.append("INSERT VALUES;")
        else:
            lines.append(line)
    text = "\n".join(lines)
raise SystemExit(0 if pattern.search(text) else 1)
PY
}

pp_backup_checksum_state() {
  local file="$1" side got want
  side="${file}.sha256"
  [[ -f "$side" && ! -L "$side" ]] || return 2
  want="$(awk 'NR==1 {print $1}' "$side")"
  got="$(sha256sum "$file" | awk '{print $1}')"
  [[ "$want" =~ ^[0-9a-f]{64}$ && "$got" == "$want" ]]
}

pp_backup_record_checksum() {
  local file="$1" stamp="$2"
  sha256sum "$file" | awk -v stamp="$stamp" 'NR==1 {print $1; print "type database"; print "created " stamp}' > "${file}.sha256"
  chmod 600 "${file}.sha256"
}

pp_backup_create() {
  pp_prepare_runtime
  cd "$PP_ROOT"
  pp_load_env_file "$PP_ROOT/config/api.env"
  pp_reject_placeholders
  local name
  for name in api web; do
    if pp_leader_matches "$name"; then
      echo "Stop the application before backup." >&2
      return 1
    fi
  done
  local stamp secret_tmp meta cnf kind target database partial
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  secret_tmp="$PP_ROOT/runtime/tmp"
  if [[ "$(id -u)" -eq 0 ]]; then
    secret_tmp="$PP_ROOT/.release/tmp"
    install -d -m 700 "$secret_tmp"
    local secret_uid secret_mode
    secret_uid="$(stat -c '%u' "$secret_tmp")"
    secret_mode="$(stat -c '%a' "$secret_tmp")"
    if [[ "$secret_uid" != "0" || $((8#$secret_mode & 022)) -ne 0 ]]; then
      echo "The backup credential directory is writable by someone other than root." >&2
      return 1
    fi
  fi
  meta="$secret_tmp/backup-meta"
  cnf="$secret_tmp/mysql-backup.cnf"
  rm -f "$meta" "$cnf"
  # A RETURN trap stays set after this function; clear it so callers do not
  # run it again without these locals.
  trap 'rm -f "$meta" "$cnf"; trap - RETURN' RETURN
  export PP_ROOT
  "$PP_ROOT/backend/.venv/bin/python" - "$stamp" "$meta" "$cnf" <<'PY'
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

root = Path(os.environ["PP_ROOT"])
stamp, meta_path, cnf_path = sys.argv[1:]
raw = os.environ["DATABASE_URL"]
dev = (root / "backend" / "data" / "partner.db").resolve()

def option(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

if raw.startswith("sqlite:"):
    source = Path(raw.removeprefix("sqlite:///"))
    if source.resolve() == dev:
        raise SystemExit("Refusing the development SQLite file.")
    target = root / "backups" / f"partnerconsole-{stamp}.db"
    shutil.copy2(source, target)
    os.chmod(target, 0o600)
    Path(meta_path).write_text(f"sqlite\n{target}\n", encoding="utf-8")
elif raw.startswith("mysql+pymysql://"):
    parts = urlparse(raw)
    password = unquote(parts.password or "")
    user = unquote(parts.username or "")
    if any(char in password or char in user for char in "\n\r"):
        raise SystemExit("Database credentials cannot contain a line break.")
    database = unquote(parts.path.lstrip("/").split("?", 1)[0])
    target = root / "backups" / f"partnerconsole-{stamp}.sql.partial"
    Path(cnf_path).write_text(
        "\n".join([
            "[client]",
            f"host={option(parts.hostname or '')}",
            f"port={parts.port or 3306}",
            f"user={option(user)}",
            f"password={option(password)}",
            "",
        ]),
        encoding="utf-8",
        newline="\n",
    )
    os.chmod(cnf_path, 0o600)
    Path(meta_path).write_text(f"mysql\n{target}\n{database}\n", encoding="utf-8")
else:
    raise SystemExit("Unsupported DATABASE_URL.")
PY
  kind="$(sed -n '1p' "$meta")"
  target="$(sed -n '2p' "$meta")"
  if [[ "$kind" == "mysql" ]]; then
    if ! command -v mysqldump >/dev/null 2>&1; then
      echo "mysqldump is a system tool and is not installed by this script." >&2
      return 1
    fi
    database="$(sed -n '3p' "$meta")"
    if ! mysqldump --defaults-extra-file="$cnf" --single-transaction --skip-lock-tables \
      --skip-add-locks --no-tablespaces --set-gtid-purged=OFF --routines --triggers \
      --default-character-set=utf8mb4 --result-file="$target" "$database"; then
      rm -f "$target"
      echo "The backup did not finish and was not kept." >&2
      return 1
    fi
    partial="$target"
    target="${target%.partial}"
    mv "$partial" "$target"
    chmod 600 "$target"
  fi
  pp_backup_record_checksum "$target" "$stamp"
  echo "Backup written inside backups/."
  echo "This is a database dump, not a full copy of the installation."
}

pp_backup_list() {
  local root file state bytes when
  root="$(pp_backup_root)"
  [[ -d "$root" ]] || { echo "No backups."; return 0; }
  printf '%s\n' "name time bytes type check"
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    pp_backup_inside "$file" || continue
    bytes="$(stat -c '%s' "$file")"
    when="$(date -u -d "@$(stat -c '%Y' "$file")" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || stat -c '%y' "$file")"
    if pp_backup_checksum_state "$file"; then
      state="valid"
    elif [[ "$?" -eq 2 ]]; then
      state="checksum-not-recorded"
    else
      state="checksum-mismatch"
    fi
    printf '%s %s %s database %s\n' "$(basename "$file")" "$when" "$bytes" "$state"
  done < <(find -P "$root" -maxdepth 1 -type f \( -name 'partnerconsole-*.sql' -o -name 'partnerconsole-*.db' \) | sort)
}

pp_backup_accept() {
  local file="$1" side mode
  side="${file}.sha256"
  pp_backup_inside "$file" || return 1
  pp_backup_inside "$side" || return 1
  pp_backup_checksum_state "$file" || return 1
  mode="$(stat -c '%a' "$file")"
  [[ $((8#$mode & 022)) -eq 0 ]] || return 1
  mode="$(stat -c '%a' "$side")"
  [[ $((8#$mode & 022)) -eq 0 ]] || return 1
  if [[ "$(id -u)" -eq 0 ]]; then
    [[ "$(stat -c '%u' "$file")" == "0" && "$(stat -c '%u' "$side")" == "0" ]] || return 1
  fi
}

pp_backup_known_version() {
  case "$1" in
    0001_initial|0002_panel_operations|0003_operation_lease|0004_subscription_identity|0005_security_state) return 0 ;;
  esac
  return 1
}

pp_backup_restore() {
  local choice file state phrase version
  [[ -r /dev/tty ]] || { echo "Restore needs a terminal and cannot be skipped." >&2; return 1; }
  echo "Restore replaces the current database. Data written after the backup is lost."
  echo "MySQL restore is not atomic. If it stops halfway, the database can stay incomplete and this command will not claim it was rolled back."
  pp_backup_list
  read -r -p "Backup number from the top, starting at 1: " choice </dev/tty
  [[ "$choice" =~ ^[0-9]+$ ]] || { echo "Choose a listed backup." >&2; return 1; }
  # awk reads the whole list. Exiting early would end the listing with SIGPIPE,
  # and pipefail would stop the restore.
  file="$(pp_backup_list | awk -v n="$choice" 'NR>1 && ++i==n {print $1}')"
  [[ -n "$file" ]] || { echo "That backup is not in the list." >&2; return 1; }
  file="$PP_ROOT/backups/$file"
  pp_backup_inside "$file" || { echo "Refusing a backup outside the backup directory." >&2; return 1; }
  if pp_backup_sql_rejected "$file"; then
    echo "Refusing this backup because it contains statements this restore will not run." >&2
    return 1
  fi
  state=0
  pp_backup_checksum_state "$file" || state=$?
  if [[ "$state" -eq 1 ]]; then
    echo "The checksum does not match. The file was not restored." >&2
    return 1
  fi
  if [[ "$state" -ne 0 ]]; then
    echo "This backup has no checksum recorded by this installer, so its origin cannot be verified. It was not restored." >&2
    echo "A v1.0.2 dump can be kept, but this command will not import it." >&2
    return 1
  fi
  pp_backup_accept "$file" || { echo "Refusing a backup that is not a root-owned checksum pair inside backups/." >&2; return 1; }
  for name in api web; do
    if pp_leader_matches "$name"; then
      echo "Stop the application before restore." >&2
      return 1
    fi
  done
  echo "A protective database dump will be taken first."
  pp_backup_create || { echo "Restore stopped because the protective backup failed." >&2; return 1; }
  read -r -p "Type restore partnerconsole to replace the database: " phrase </dev/tty
  [[ "$phrase" == "restore partnerconsole" ]] || { echo "Restore cancelled." >&2; return 1; }
  if ! pp_backup_trial_import "$file"; then
    echo "The trial import failed. The main database was not changed." >&2
    return 1
  fi
  if ! pp_backup_import_main "$file"; then
    echo "The restore did not finish. The database may be incomplete. Use the protective backup in backups/. The application was not started." >&2
    return 1
  fi
  version="$(pp_backup_alembic_version partnerconsole || true)"
  pp_backup_known_version "$version" || {
    echo "The restored database version is not one this release can run. The application was not started." >&2
    return 1
  }
  echo "Restore finished. Start the application only after you have checked the data."
}

pp_backup_alembic_version() {
  local schema="$1"
  pp_backup_root_mysql -N -e "SELECT version_num FROM \`${schema}\`.alembic_version LIMIT 1"
}

pp_backup_root_mysql() {
  mysql --protocol=socket "$@"
}

pp_backup_client_mysql() {
  mysql "$@"
}

pp_backup_dump_locks() {
  grep -qE '^LOCK TABLES[[:space:]]' "$1"
}

pp_backup_main_lock_grant() {
  printf '%s\n' "GRANT LOCK TABLES ON \`partnerconsole\`.* TO 'partnerconsole'@'127.0.0.1'"
}

pp_backup_grants_are_limited() {
  local line saw=0
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    case "$line" in
      "GRANT USAGE ON *.* TO "*) ;;
      "GRANT "*" ON \`partnerconsole_restore_check\`.* TO "*|"GRANT "*" ON partnerconsole_restore_check.* TO "*)
        case "$line" in
          *"LOCK TABLES"*) saw=1 ;;
          *) return 1 ;;
        esac
        ;;
      *) return 1 ;;
    esac
  done
  [[ "$saw" == "1" ]]
}

pp_backup_trial_reserved() {
  local db user
  db="$(pp_backup_root_mysql -N -e "SHOW DATABASES LIKE 'partnerconsole_restore_check'")"
  user="$(pp_backup_root_mysql -N -e "SELECT user FROM mysql.user WHERE user='pc_restore_check'")"
  [[ -n "$db" || -n "$user" ]]
}

pp_backup_trial_cleanup() {
  local created_db="$1" created_user="$2"
  if [[ "$created_user" == "1" ]]; then
    pp_backup_root_mysql -e "DROP USER 'pc_restore_check'@'127.0.0.1'" || echo "The trial account created by this run could not be removed." >&2
  fi
  if [[ "$created_db" == "1" ]]; then
    pp_backup_root_mysql -e "DROP DATABASE partnerconsole_restore_check" || echo "The trial database created by this run could not be removed." >&2
  fi
}

pp_backup_main_fingerprint() {
  pp_backup_root_mysql -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='partnerconsole'; SELECT COUNT(*) FROM partnerconsole.admin_users;"
}

pp_backup_trial_import() {
  local file="$1" pass cnf version="" created_db=0 created_user=0 before after grants
  if pp_backup_trial_reserved; then
    echo "A trial database or account already exists. It was not changed." >&2
    return 1
  fi
  pass="$(pp_backup_python -c 'import secrets; print(secrets.token_urlsafe(18))')"
  cnf="$(mktemp)"
  chmod 600 "$cnf"
  pp_backup_trial_close() {
    rm -f "$cnf"
    pp_backup_trial_cleanup "$created_db" "$created_user"
  }
  before="$(pp_backup_main_fingerprint || true)"
  if ! pp_backup_root_mysql -e "CREATE DATABASE partnerconsole_restore_check CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"; then
    echo "The trial database could not be created. Nothing else was changed." >&2
    pp_backup_trial_close
    return 1
  fi
  created_db=1
  if pp_backup_root_mysql -N -e "SELECT user FROM mysql.user WHERE user='pc_restore_check'" | grep -q .; then
    echo "The trial account already exists. The database created by this run will be removed and the existing account was not changed." >&2
    pp_backup_trial_close
    return 1
  fi
  if ! pp_backup_root_mysql -e "CREATE USER 'pc_restore_check'@'127.0.0.1' IDENTIFIED BY '${pass}'"; then
    echo "The trial account could not be created. Only the trial database from this run will be removed." >&2
    pp_backup_trial_close
    return 1
  fi
  created_user=1
  if ! pp_backup_root_mysql -e "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, LOCK TABLES ON partnerconsole_restore_check.* TO 'pc_restore_check'@'127.0.0.1'"; then
    echo "The trial account could not be limited to its own database." >&2
    pp_backup_trial_close
    return 1
  fi
  grants="$(pp_backup_root_mysql -N -e "SHOW GRANTS FOR 'pc_restore_check'@'127.0.0.1'" || true)"
  if ! printf '%s\n' "$grants" | pp_backup_grants_are_limited; then
    echo "The trial account was not limited to partnerconsole_restore_check. The import did not start." >&2
    pp_backup_trial_close
    return 1
  fi
  printf '%s' "$pass" | pp_backup_python -c 'import os, sys; from pathlib import Path; p=Path(sys.argv[1]); pw=sys.stdin.read().replace("\\\\", "\\\\\\\\").replace("\"", "\\\\\""); p.write_text("[client]\nhost=\"127.0.0.1\"\nport=3306\nuser=\"pc_restore_check\"\npassword=\"%s\"\n" % pw, encoding="utf-8"); os.chmod(p, 0o600)' "$cnf"
  unset pass
  if pp_backup_client_mysql --defaults-extra-file="$cnf" -N -e "SELECT 1 FROM partnerconsole.admin_users" >/dev/null 2>&1; then
    echo "The trial account can read the main database. The import did not start." >&2
    pp_backup_trial_close
    return 1
  fi
  if ! pp_backup_client_mysql --defaults-extra-file="$cnf" --binary-mode --local-infile=0 partnerconsole_restore_check < "$file"; then
    echo "The trial import did not finish. The main database was not changed by this command." >&2
    pp_backup_trial_close
    return 1
  fi
  version="$(pp_backup_client_mysql --defaults-extra-file="$cnf" -N partnerconsole_restore_check -e "SELECT version_num FROM alembic_version LIMIT 1" || true)"
  after="$(pp_backup_main_fingerprint || true)"
  if [[ "$before" != "$after" ]]; then
    echo "The trial import changed the main database." >&2
    pp_backup_trial_close
    return 1
  fi
  if ! pp_backup_known_version "$version"; then
    echo "The trial database version is not one this release can run. The main database was not changed." >&2
    pp_backup_trial_close
    return 1
  fi
  pp_backup_trial_close
}

pp_backup_import_main() {
  local file="$1"
  pp_prepare_runtime
  pp_load_env_file "$PP_ROOT/config/api.env"
  local secret_tmp="$PP_ROOT/.release/tmp" cnf
  install -d -m 700 "$secret_tmp"
  cnf="$secret_tmp/mysql-restore.cnf"
  export PP_ROOT
  "$PP_ROOT/backend/.venv/bin/python" - "$cnf" <<'PY'
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
raw = ""
for line in Path(os.environ["PP_ROOT"], "config/api.env").read_text(encoding="utf-8").splitlines():
    if line.startswith("DATABASE_URL="):
        raw = line.split("=", 1)[1]
parts = urlparse(raw)

def option(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

target = Path(sys.argv[1])
target.write_text(
    "\n".join([
        "[client]",
        f"host={option(parts.hostname or '127.0.0.1')}",
        f"port={parts.port or 3306}",
        f"user={option(unquote(parts.username or ''))}",
        f"password={option(unquote(parts.password or ''))}",
        "",
    ]),
    encoding="utf-8",
    newline="\n",
)
os.chmod(target, 0o600)
PY
  local database
  database="$("$PP_ROOT/backend/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
raw = ""
for line in Path(os.environ["PP_ROOT"], "config/api.env").read_text(encoding="utf-8").splitlines():
    if line.startswith("DATABASE_URL="):
        raw = line.split("=", 1)[1]
print(unquote(urlparse(raw).path.lstrip("/").split("?", 1)[0]))
PY
)"
  [[ "$database" == "partnerconsole" ]] || { rm -f "$cnf"; echo "Refusing to restore into an unexpected database." >&2; return 1; }
  local db_user
  db_user="$("$PP_ROOT/backend/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
from urllib.parse import unquote, urlparse
raw = ""
for line in Path(os.environ["PP_ROOT"], "config/api.env").read_text(encoding="utf-8").splitlines():
    if line.startswith("DATABASE_URL="):
        raw = line.split("=", 1)[1]
user = unquote(urlparse(raw).username or "")
if user != "partnerconsole":
    raise SystemExit(1)
print(user)
PY
)" || { rm -f "$cnf"; echo "Refusing to change privileges for an unexpected database user." >&2; return 1; }
  [[ "$db_user" == "partnerconsole" ]] || { rm -f "$cnf"; echo "Refusing to change privileges for an unexpected database user." >&2; return 1; }
  # Older dumps contain LOCK TABLES. New dumps use --skip-add-locks and skip this.
  # The added privilege is only LOCK TABLES on partnerconsole, for this database user.
  if pp_backup_dump_locks "$file"; then
    pp_backup_root_mysql -e "$(pp_backup_main_lock_grant)" || {
      rm -f "$cnf"
      echo "The database user could not be allowed to import a dump that locks tables. The main database was not changed." >&2
      return 1
    }
  fi
  local status=0
  mysql --defaults-extra-file="$cnf" --binary-mode --local-infile=0 "$database" < "$file" || status=$?
  rm -f "$cnf"
  return "$status"
}

if [[ "${PP_BACKUP_LIBRARY:-}" != "1" && "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-create}" in
    create) pp_backup_create ;;
    list) pp_backup_list ;;
    restore) pp_backup_restore ;;
    *) echo "Use create, list, or restore." >&2; exit 2 ;;
  esac
fi
