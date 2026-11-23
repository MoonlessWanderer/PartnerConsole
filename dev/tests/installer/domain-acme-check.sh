#!/usr/bin/env bash
set -euo pipefail
umask 077

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
# shellcheck disable=SC1091
source "$repo/scripts/domain.sh"

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
PP_ACME_ROOT="$work/acme"
challenge="$PP_ACME_ROOT/.well-known/acme-challenge"
mkdir -p "$challenge"
printf 'existing challenge\n' > "$challenge/existing-token"
printf 'old probe\n' > "$challenge/pc-acme-probe"

curl() {
  local response='' url='' host='' count token
  while (($#)); do
    case "$1" in
      --output) response="$2"; shift ;;
      -H) host="$2"; shift ;;
      http://*) url="$1" ;;
    esac
    shift
  done
  [[ "$host" == 'Host: console.example.test' && -n "$response" ]] || fail 'request options'
  [[ "$url" == http://127.0.0.1/.well-known/acme-challenge/pc-acme-probe.* ]] || fail 'probe URL'
  token="${url##*/}"
  [[ -f "$challenge/$token" ]] || fail 'probe is missing'
  printf '%s\n' "$token" "$response" > "$work/last-files"
  count="$(cat "$work/calls")"
  count=$((count + 1))
  echo "$count" > "$work/calls"
  case "$scenario" in
    delayed)
      if [[ "$count" == 1 ]]; then printf '404'; return 0; fi
      cat "$challenge/$token" > "$response"
      printf '200'
      ;;
    wrong-body) printf '%s\n' "$token" > "$response"; printf '200' ;;
    forbidden) printf '403' ;;
    not-found) printf '404' ;;
    redirect) printf '301' ;;
    server-error) printf '500' ;;
    bad-gateway) printf '502' ;;
    unavailable) printf '503' ;;
    timeout) return 28 ;;
    refused) return 7 ;;
  esac
}
sleep() { [[ "$*" == 1 ]] || fail 'unbounded retry delay'; }

scenario=delayed
echo 0 > "$work/calls"
pp_domain_acme_probe console.example.test
[[ "$(cat "$work/calls")" == 2 ]] || fail 'probe did not retry transient failure'
first_token="$(head -n1 "$work/last-files")"

for scenario in wrong-body forbidden not-found redirect server-error bad-gateway unavailable timeout refused; do
  echo 0 > "$work/calls"
  if pp_domain_acme_probe console.example.test > "$work/output" 2>&1; then fail "accepted $scenario"; fi
  [[ "$(cat "$work/calls")" == 5 ]] || fail "retry limit for $scenario"
  grep -Fq 'failed after 5 attempts' "$work/output" || fail "error message for $scenario"
  [[ "$(head -n1 "$work/last-files")" != "$first_token" ]] || fail 'probe token was reused'
  [[ ! -e "$(tail -n1 "$work/last-files")" ]] || fail 'response fixture was left behind'
done
[[ "$(find "$challenge" -maxdepth 1 -type f | wc -l)" == 2 ]] || fail 'probe files were left behind'
[[ "$(cat "$challenge/existing-token")" == 'existing challenge' ]] || fail 'unrelated token changed'
[[ "$(cat "$challenge/pc-acme-probe")" == 'old probe' ]] || fail 'previous probe changed'
[[ "$(umask)" == 0077 ]] || fail 'caller umask changed'
printf 'ok ACME retries, exact byte matching, bounded failures, unique names and cleanup\n'
