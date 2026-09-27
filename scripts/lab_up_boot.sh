#!/usr/bin/env bash
# Wrapper for @reboot cron. Everything cron makes awkward lives here rather than
# in the crontab line: absolute paths, a timestamped log, and a recorded exit code.
#
# Kept separate from lab_up.py because a crontab entry is not a place you can read,
# test, or diff. This is.
#
#   ./scripts/lab_up_boot.sh          # what cron runs
#   tail -f ~/lab_up.log              # watch a boot
#
# Note: cron's date is not the script's problem, but the log's is -- lab_up.py
# stamps HH:MM:SS only, which is ambiguous across reboots. The banner supplies
# the date.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${LAB_UP_LOG:-$HOME/lab_up.log}"

# No RTC on the Pi: until NTP syncs, date is whatever fake-hwclock last saved.
# Wait (bounded) for sync so the banner is truthful; lab_up.py itself uses a
# monotonic clock and does not depend on this.
for _ in $(seq 1 24); do
    [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && break
    sleep 5
done

{
    echo "=== boot $(date -Is) ==="
    "$ROOT/.venv/bin/python" "$ROOT/scripts/lab_up.py"
    rc=$?
    echo "=== exit $rc ==="
    echo
} >>"$LOG" 2>&1

exit "$rc"
