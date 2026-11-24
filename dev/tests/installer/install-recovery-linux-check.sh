#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "$EUID" == 0 && "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]]
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
tools="$(cd "$(dirname "$0")" && pwd)"
assets="$(realpath "$1")"
commit="$2"
scenario="$3"
app=/srv/partnerconsole
[[ ! -e "$app" && ! -e /usr/local/bin/partnerconsole ]]
tag="$(sed -n 's/^RELEASE_TAG="\(.*\)"$/\1/p' "$assets/partnerconsole.sh")"
# shellcheck source=/dev/null
source "$tools/release-http-fixture.sh"
setup_release_feed
cp -r "$assets" "$PC_RELEASE_FIXTURE_DIR/$tag"
publish_fixture "$tag"
work="$(mktemp -d)"
mkdir "$work/empty"
cd "$work/empty"
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$work/password"
python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$work/another-password"

readme_install() {
  python3 "$tools/release-tty.py" --mode "$1" "$2" bash "$tools/user-install.sh"
}
check_admin_login() {
  python3 - "$work/password" "$work/another-password" <<'PY'
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

for file, expected in zip(sys.argv[1:], (200, 401), strict=True):
    body = json.dumps({"username": "admin", "password": Path(file).read_text().strip()}).encode()
    req = Request("http://127.0.0.1:8741/api/admin/auth/login", data=body,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as result:
            assert result.status == expected
            assert json.load(result)["access_token"]
    except HTTPError as error:
        assert error.code == expected, error.code
PY
}
no_application_state() {
  local database
  database="$(mysql --protocol=socket -N -e "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='partnerconsole'")"
  if [[ "$database" != 0 || -e "$app/config/api.env" || -e "$app/config/web.env" || -e "$app/.release/current" ]]; then
    printf 'بازتولید خرابی %s از مسیر %s، شمار دیتابیس=%s، تنظیمات API=%s، تنظیمات وب=%s، ثبت نسخه=%s\n' \
      "$scenario" "$1" "$database" \
      "$(test -e "$app/config/api.env" && echo yes || echo no)" \
      "$(test -e "$app/config/web.env" && echo yes || echo no)" \
      "$(test -e "$app/.release/current" && echo yes || echo no)" >&2
    return 1
  fi
}

case "$scenario" in
  resume)
    readme_install interrupt "$work/password"
    grep -Fx "commit=$commit" "$app/.release/current"
    admins="$(mysql --protocol=socket -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='partnerconsole' AND table_name='admin_users'")"
    if [[ "$admins" == 1 ]]; then
      [[ "$(mysql --protocol=socket -N -e 'SELECT COUNT(*) FROM partnerconsole.admin_users')" == 0 ]]
    else
      [[ "$admins" == 0 ]]
    fi
    cp "$app/config/api.env" "$app/config/web.env" "$app/.release/current" "$work/"
    if ! readme_install resume "$work/password" > "$work/resume.log" 2>&1; then
      tail -20 "$work/resume.log"
      echo 'بازتولید خرابی ادامهٔ نصب، با وجود ثبت نسخه و نبود مدیر، دستور README نصب را کامل نکرد.' >&2
      exit 1
    fi
    grep -F 'Database bootstrap completed.' "$work/resume.log"
    cmp "$work/api.env" "$app/config/api.env"
    cmp "$work/web.env" "$app/config/web.env"
    cmp "$work/current" "$app/.release/current"
    check_admin_login
    mysql --protocol=socket -N -e 'SELECT id, password_hash FROM partnerconsole.admin_users ORDER BY id' > "$work/admin-before"
    [[ "$(wc -l < "$work/admin-before")" == 1 ]]
    readme_install repeat "$work/another-password"
    mysql --protocol=socket -N -e 'SELECT id, password_hash FROM partnerconsole.admin_users ORDER BY id' > "$work/admin-after"
    cmp "$work/admin-before" "$work/admin-after"
    cmp "$work/api.env" "$app/config/api.env"
    cmp "$work/web.env" "$app/config/web.env"
    check_admin_login
    echo 'ادامهٔ نصب با دستور README، حفظ تنظیمات، ورود مدیر و تکرار بدون تغییر حساب یا رمز موفق شدند.'
    ;;
  download|checksum)
    archive="$PC_RELEASE_FIXTURE_DIR/$tag/partnerconsole-$tag.tar.gz"
    mv "$archive" "$work/good.tar.gz"
    expected="Could not download the $tag application package"
    if [[ "$scenario" == checksum ]]; then
      printf 'bad package bytes\n' > "$archive"
      expected="Application package checksum mismatch for $tag"
    fi
    cp "$assets/partnerconsole.sh" ./partnerconsole.sh
    # Redirection on the already-root disposable host is intentional.
    # shellcheck disable=SC2024
    if sudo bash ./partnerconsole.sh install > "$work/direct.log" 2>&1; then
      echo 'دستور مستقیم با بستهٔ نامعتبر به‌اشتباه موفق شد.' >&2
      exit 1
    fi
    grep -F "$expected" "$work/direct.log"
    no_application_state direct
    python3 "$tools/release-tty.py" --mode menu "$work/password" sudo bash ./partnerconsole.sh > "$work/menu.log" 2>&1
    grep -F "$expected" "$work/menu.log"
    grep -F 'Failed.' "$work/menu.log"
    no_application_state menu
    mv "$work/good.tar.gz" "$archive"
    readme_install install "$work/password"
    grep -Fx "commit=$commit" "$app/.release/current"
    check_admin_login
    echo 'توقف پیش از ایجاد داده در هر دو مسیر و نصب موفق پس از بازگرداندن بستهٔ سالم تأیید شدند.'
    ;;
  *) exit 2 ;;
esac
