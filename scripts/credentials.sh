#!/usr/bin/env bash
# Change one existing admin. Password hashing is the application's own function.
# A new password does not revoke JWTs: sessions carry the admin id, and the
# existing maintenance command says to rotate ADMIN_JWT_SECRET for that.

pp_credentials_python() {
  local status=0
  (cd / && "$PP_ROOT/backend/.venv/bin/python" -I "$@" 2>/dev/null) || status=$?
  # Validation errors are printed explicitly. Other Python errors can contain configuration values.
  if [[ "$status" != 0 && "$status" != 2 ]]; then
    echo "Credentials operation failed. Check the API configuration and MySQL connection." >&2
  fi
  return "$status"
}

pp_credentials_check_database() {
  pp_credentials_python - "$DB_USER" "$DB_NAME" <<'PY'
import os
import sys

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

try:
    url = make_url(os.environ["DATABASE_URL"])
except (ArgumentError, ValueError):
    url = None
if (url is None or url.drivername != "mysql+pymysql" or url.username != sys.argv[1]
        or not url.password or url.host not in {"127.0.0.1", "localhost", "::1"}
        or (url.port is not None and url.port != 3306) or url.database != sys.argv[2]
        or url.query.keys() - {"charset"}):
    print("DATABASE_URL must select the installed local MySQL database.")
    raise SystemExit(2)
PY
}

pp_credentials_list() {
  pp_credentials_python - <<'PY'
from sqlalchemy import select
from app.core.database import SessionLocal
from app.models import AdminUser
with SessionLocal() as db:
    rows = db.scalars(select(AdminUser).order_by(AdminUser.id)).all()
    if not rows:
        print("No administrator exists.")
        raise SystemExit(2)
    for row in rows:
        print(f"{row.id} {row.username}")
PY
}

pp_credentials_rename() {
  pp_credentials_python - "$1" "$2" "$PP_ROOT/config/api.env" <<'PY'
import sys
from pathlib import Path

from app.core.database import SessionLocal
from app.reset_admin_password import rename_admin, validate_admin_username

try:
    admin_id = int(sys.argv[1])
    username = validate_admin_username(sys.argv[2])
    with SessionLocal() as db:
        rename_admin(db, admin_id, username, config_path=Path(sys.argv[3]))
except ValueError as exc:
    print(exc)
    raise SystemExit(2) from None
print(f"Administrator {admin_id} is now {username}. Existing sessions still use the same account id.")
PY
}

pp_credentials_password() {
  local admin_id="$1" first second
  pp_credentials_python - "$admin_id" <<'PY' || return 1
import sys
from app.core.database import SessionLocal
from app.models import AdminUser

with SessionLocal() as db:
    if db.get(AdminUser, int(sys.argv[1])) is None:
        print("Admin not found.")
        raise SystemExit(2)
PY
  echo "Existing sessions stay signed in."
  read -r -s -p "New password: " first </dev/tty || return 1
  printf '\n'
  read -r -s -p "Repeat password: " second </dev/tty || return 1
  printf '\n'
  [[ "$first" == "$second" ]] || { echo "The passwords do not match." >&2; return 1; }
  printf '%s' "$first" | pp_credentials_python -c '
import sys
from app.core.database import SessionLocal
from app.reset_admin_password import set_admin_password_by_id

try:
    with SessionLocal() as db:
        set_admin_password_by_id(db, int(sys.argv[1]), sys.stdin.read())
except ValueError as exc:
    print(exc)
    raise SystemExit(2) from None
' "$admin_id" || return 1
  echo "Password updated. Existing sessions stay valid until they expire or you rotate ADMIN_JWT_SECRET."
}

pp_credentials_menu() {
  local choice admin_id username
  ( : </dev/tty ) 2>/dev/null || { echo "Admin credentials need a terminal." >&2; return 1; }
  pp_credentials_check_database || return 1
  while true; do
    printf '\nAdmin Credentials\n\n  1) Change Admin Username\n  2) Change Admin Password\n  0) Back\n\n'
    read -r -p "Choice: " choice </dev/tty || return 1
    case "$choice" in
      1|2)
        pp_credentials_list || return 1
        read -r -p "Admin id: " admin_id </dev/tty || return 1
        [[ "$admin_id" =~ ^[0-9]+$ ]] || { echo "Choose an administrator by id." >&2; continue; }
        if [[ "$choice" == "1" ]]; then
          read -r -p "New username: " username </dev/tty || return 1
          pp_credentials_rename "$admin_id" "$username" || true
        else
          pp_credentials_password "$admin_id" || true
        fi
        ;;
      0) return 0 ;;
      *) echo "Invalid choice." ;;
    esac
  done
}
