#!/usr/bin/env bash
# Run after the upgrade and browser purchase on the same disposable runner.
set -euo pipefail
umask 077
[[ "$EUID" == 0 && "${PARTNERCONSOLE_DISPOSABLE_HOST:-}" == 1 ]]
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]]
app=/srv/partnerconsole
[[ -f "$app/.release/current" && -x /usr/local/bin/partnerconsole ]]
work="$(mktemp -d)"
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok %s\n' "$*"; }
root_sql() { mysql --protocol=socket -N -e "$1"; }

cat > "$work/answer.py" <<'PY'
import os, pty, re, select, signal, sys, time
args = sys.argv[1:]
split = args.index("--")
pairs = list(zip(args[:split:2], args[1:split:2]))
command = args[split + 1:]
pid, fd = pty.fork()
if pid == 0:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    os.execvp(command[0], command)
seen = b""
deadline = time.time() + 900
while time.time() < deadline:
    ready, _, _ = select.select([fd], [], [], 1)
    if not ready:
        continue
    try:
        data = os.read(fd, 4096)
    except OSError:
        break
    if not data:
        break
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    seen += data
    if pairs and re.search(re.escape(pairs[0][0]).encode(), seen):
        os.write(fd, pairs.pop(0)[1].encode() + b"\n")
        seen = b""
else:
    os.kill(pid, 9)
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
PY
db_state() {
  local table
  root_sql "SELECT table_name FROM information_schema.tables WHERE table_schema='partnerconsole' ORDER BY table_name" > "$work/tables"
  printf 'tables %s\n' "$(tr '\n' ' ' < "$work/tables")"
  while IFS= read -r table; do
    root_sql "CHECKSUM TABLE \`partnerconsole\`.\`$table\` EXTENDED"
  done < "$work/tables"
  root_sql "SELECT version_num FROM partnerconsole.alembic_version"
  root_sql "SELECT username, balance_toman, banned FROM partnerconsole.partners ORDER BY username"
}

# Full Restore with fixture data.
restore() {
  local choice="$1" output="$2"
  sleep 1
  python3 "$work/answer.py" \
    'Backup number from the top, starting at 1: ' "$choice" \
    'Type restore partnerconsole to replace the database: ' 'restore partnerconsole' \
    -- bash "$app/scripts/backup.sh" restore > "$output" 2>&1
}
list_index() {
  bash "$app/scripts/backup.sh" list | awk -v name="$1" 'NR > 1 {i++; if ($1 == name) print i}'
}
trial_left() {
  [[ -n "$(root_sql "SHOW DATABASES LIKE 'partnerconsole_restore_check'")$(root_sql "SELECT user FROM mysql.user WHERE user='pc_restore_check'")" ]]
}

sudo partnerconsole stop || fail 'official Stop'
purchase_count() {
  root_sql "SELECT COUNT(*) FROM partnerconsole.wallet_ledgers l
    JOIN partnerconsole.subscriptions s ON s.id=l.subscription_id
    WHERE s.username='fixture_after_update' AND l.reason='create' AND l.asset='toman' AND l.amount=-5300"
}
[[ "$(purchase_count)" == 1 ]] || fail 'browser purchase ledger before Backup'
db_state > "$work/state-backup"
grep -Fxq '0005_security_state' "$work/state-backup" || fail 'unexpected migration version before backup'
/usr/local/bin/partnerconsole backup > "$work/backup.log" 2>&1 || fail "official Backup: $(tail -n 5 "$work/backup.log")"
mapfile -t dumps < <(find "$app/backups" -maxdepth 1 -name 'partnerconsole-*.sql' -printf '%f\n' | sort)
[[ "${#dumps[@]}" == 1 ]] || fail 'Backup did not write exactly one dump'
backup_name="${dumps[0]}"
[[ "$(stat -c '%u:%a' "$app/backups/$backup_name" "$app/backups/$backup_name.sha256" | sort -u)" == 0:600 ]] || fail 'backup files are not root:0600'
bash "$app/scripts/backup.sh" list | grep -Eq "^$backup_name .* database valid$" || fail 'the backup checksum is not valid'
ok 'official Backup wrote a root-only MySQL dump with a valid checksum'

root_sql "UPDATE partnerconsole.partners SET balance_toman = 999999 WHERE username = 'fixture_upgrade_a';
  INSERT INTO partnerconsole.partners (username, password_hash, balance_toman, banned, created_at, updated_at)
  VALUES ('restore-mutation', 'fixture-not-a-hash', 42, 0, NOW(6), NOW(6))"
db_state > "$work/state-changed"
! cmp -s "$work/state-backup" "$work/state-changed" || fail 'the data change after Backup was not visible'

record_checksum() {
  (
    PP_BACKUP_LIBRARY=1
    # shellcheck disable=SC1091
    source "$app/scripts/backup.sh"
    pp_backup_record_checksum "$1" "$2"
  )
}
broken="$app/backups/partnerconsole-20000101T000000Z.sql"
{ cat "$app/backups/$backup_name"; printf 'INSERT INTO pc_missing_table VALUES (1);\n'; } > "$broken"
record_checksum "$broken" 20000101T000000Z
restore "$(list_index "$(basename "$broken")")" "$work/restore-broken" && fail 'a dump that fails to import was restored'
grep -Fq 'The trial import failed. The main database was not changed.' "$work/restore-broken" || { cat "$work/restore-broken" >&2; fail 'trial import failure message'; }
db_state | cmp -s - "$work/state-changed" || fail 'a failed trial import changed the main database'
if trial_left; then fail 'the trial database or account was left behind'; fi
ok 'a dump that fails to import stops at the trial import and leaves the main database unchanged'

tampered="$app/backups/partnerconsole-20000102T000000Z.sql"
cp "$app/backups/$backup_name" "$tampered"
record_checksum "$tampered" 20000102T000000Z
printf -- '-- changed after the checksum\n' >> "$tampered"
restore "$(list_index "$(basename "$tampered")")" "$work/restore-tampered" && fail 'a changed dump was restored'
grep -Fq 'The checksum does not match. The file was not restored.' "$work/restore-tampered" || { cat "$work/restore-tampered" >&2; fail 'checksum mismatch message'; }
db_state | cmp -s - "$work/state-changed" || fail 'a checksum mismatch changed the main database'
ok 'a dump changed after its checksum is refused before any import'

protective_before="$(find "$app/backups" -maxdepth 1 -name 'partnerconsole-20[1-9]*.sql' | wc -l)"
restore "$(list_index "$backup_name")" "$work/restore-good" || { cat "$work/restore-good" >&2; fail 'official Full Restore'; }
grep -Fq 'Restore finished.' "$work/restore-good" || fail 'Restore did not report success'
db_state | cmp -s - "$work/state-backup" || { diff <(db_state) "$work/state-backup" >&2 || true; fail 'restored data differs from the backup'; }
if trial_left; then fail 'the trial database or account was left behind after Restore'; fi
newest="$(find "$app/backups" -maxdepth 1 -name 'partnerconsole-20[1-9]*.sql' -printf '%f\n' | sort | tail -n 1)"
[[ "$(find "$app/backups" -maxdepth 1 -name 'partnerconsole-20[1-9]*.sql' | wc -l)" -gt "$protective_before" ]] || fail 'Restore took no protective backup'
grep -Fq "'restore-mutation'" "$app/backups/$newest" || fail 'the protective backup does not hold the data from before Restore'
[[ "$(purchase_count)" == 1 ]] || fail 'browser purchase ledger after Restore'
ok 'official Full Restore returned every table to the backup, including the browser purchase and its ledger; the protective backup was retained'

sudo partnerconsole start || fail 'start after Restore'
[[ "$(curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1:8741/api/health)" == 200 ]] || fail 'API health after Restore'
ok 'the application starts on the restored database'
printf 'backup and full restore after the browser purchase passed\n'
