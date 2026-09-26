#!/usr/bin/env bash
# Show which subnet manager is MASTER and which is STANDBY -- the UFM HA view.
#
#   ibfabric/sm_status.sh
#
# Queries the SM from each SM host's own port, so a host whose OpenSM has died
# shows up as "no SM answering" rather than silently disappearing.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/ibenv.sh"

for host in ufm1 ufm2; do
    pid=$(pgrep -o -f "opensm.*opensm-$host.log" || true)
    printf '%-5s opensm %-12s ' "$host" "${pid:+pid $pid}"
    [[ -n "$pid" ]] || printf 'DOWN        '
    lid=$(SIM_HOST=$host ibstat 2>/dev/null | awk '/Base lid/{print $3; exit}')
    [[ -n "$lid" && "$lid" != 0 ]] || { echo "(no LID yet)"; continue; }
    # sminfo against this host's own LID reports that SM's state directly.
    SIM_HOST=$host sminfo "$lid" 2>/dev/null | sed -E 's/.*priority ([0-9]+) state [0-9]+ (SMINFO_[A-Z]+).*/priority \1  \2/' \
        || echo "no SM answering"
done
