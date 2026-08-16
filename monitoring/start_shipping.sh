#!/usr/bin/env bash
# Validate ~/.grafana.env, start Alloy, and confirm Grafana Cloud accepts the push.
#
# Never prints the token. Long tokens pasted over a mobile SSH session are the
# usual failure here, so the checks below look for exactly that: placeholders
# left in, embedded whitespace, a wrapped line, a truncated value.

set -uo pipefail

ENV_FILE="$HOME/.grafana.env"
FAIL=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok       %s\n' "$*"; }
bad()  { printf '  PROBLEM  %s\n' "$*"; FAIL=1; }

say "=== checking $ENV_FILE ==="

[ -f "$ENV_FILE" ] || { bad "file does not exist"; exit 1; }

perms=$(stat -c '%a' "$ENV_FILE")
[ "$perms" = "600" ] && ok "permissions are 600" || bad "permissions are $perms, want 600 (chmod 600 $ENV_FILE)"

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

check_var() {
  local name=$1 val=${!1-} minlen=$2
  if [ -z "$val" ]; then
    bad "$name is empty"
  elif [[ "$val" == PASTE_* ]]; then
    bad "$name still has the placeholder in it"
  elif [[ "$val" =~ [[:space:]] ]]; then
    bad "$name contains a space or newline -- the paste wrapped; re-paste it as one line"
  elif [ "${#val}" -lt "$minlen" ]; then
    bad "$name is only ${#val} chars, expected at least $minlen -- looks truncated"
  else
    ok "$name looks well-formed (${#val} chars)"
  fi
}

check_var GRAFANA_PROM_URL 20
check_var GRAFANA_PROM_USER 4
check_var GRAFANA_PROM_TOKEN 32

case "${GRAFANA_PROM_URL-}" in
  https://*/api/prom/push) ok "URL has the expected .../api/prom/push shape" ;;
  https://*)               bad "URL should normally end in /api/prom/push" ;;
  *)                       bad "URL should start with https://" ;;
esac

[[ "${GRAFANA_PROM_USER-}" =~ ^[0-9]+$ ]] \
  && ok "user ID is numeric" \
  || bad "user ID should be all digits (it is the instance ID, not your email)"

[ "$FAIL" -eq 0 ] || { say ""; say "Fix the above, then run this again. Nothing was started."; exit 1; }

say ""
say "=== starting Alloy ==="
systemctl --user enable --now alloy >/dev/null 2>&1
sleep 20

if ! systemctl --user is-active --quiet alloy; then
  say "Alloy is not running. Last few log lines:"
  journalctl --user -u alloy -n 15 --no-pager
  exit 1
fi
ok "service is active"

say ""
say "=== is Grafana Cloud accepting the data? ==="
M=$(curl -s --max-time 15 http://127.0.0.1:12345/metrics)
sent=$(  printf '%s' "$M" | awk '/^prometheus_remote_storage_samples_total/        {s+=$2} END{printf "%d", s}')
failed=$(printf '%s' "$M" | awk '/^prometheus_remote_storage_samples_failed_total/ {s+=$2} END{printf "%d", s}')

say "  samples sent:   ${sent:-0}"
say "  samples failed: ${failed:-0}"

if [ "${sent:-0}" -gt 0 ] && [ "${failed:-0}" -eq 0 ]; then
  say ""
  say "Shipping. Give Grafana a minute, then query  cml_up  in Explore."
elif [ "${failed:-0}" -gt 0 ]; then
  say ""
  say "Grafana Cloud is rejecting the writes -- usually a wrong token or user ID."
  journalctl --user -u alloy -n 15 --no-pager | grep -i 'error\|401\|403\|429' | tail -5
  exit 1
else
  say ""
  say "Nothing sent yet. Not necessarily wrong -- the first push can take ~60s."
  say "Re-run this script, or watch:  journalctl --user -u alloy -f"
fi
