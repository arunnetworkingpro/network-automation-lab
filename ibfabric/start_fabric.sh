#!/usr/bin/env bash
# Bring up the simulated InfiniBand fabric: ibsim + OpenSM, both unprivileged.
#
#   ibfabric/start_fabric.sh [netlist]
#
# ibsim runs in remote mode (-r) so hosts other than the Pi -- the CML Linux
# nodes -- can attach as HCAs over UDP 7070+. Every IB tool has to be run with
# the umad2sim shim preloaded; source ibfabric/ibenv.sh to get that.
#
# Everything writes under $RUN so nothing needs root: OpenSM defaults its cache
# to /var/cache/opensm and dies with IB_INSUFFICIENT_RESOURCES if it cannot
# create it, which reads like a fabric problem and is not one.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NETLIST="${1:-$HERE/netlists/fat-tree.net}"
RUN="${IBFABRIC_RUN:-$HOME/.local/share/ibfabric}"

export OSM_CACHE_DIR="$RUN/cache"
mkdir -p "$OSM_CACHE_DIR" "$RUN/log"

[[ -f "$NETLIST" ]] || { echo "no netlist at $NETLIST -- run gen_netlist.py" >&2; exit 1; }

# The shim's path is arch-dependent; find it rather than hardcode aarch64.
SHIM="$(find /usr/lib -name libumad2sim.so -print -quit 2>/dev/null)"
[[ -n "$SHIM" ]] || { echo "libumad2sim.so not found -- install ibsim-utils" >&2; exit 1; }

pkill -x opensm 2>/dev/null || true
pkill -x ibsim  2>/dev/null || true

echo "starting ibsim on $(basename "$NETLIST")"
# -s starts the network immediately, which -n (no console) requires.
ibsim -r -s -n -N 512 -S 64 -P 8192 "$NETLIST" > "$RUN/log/ibsim.log" 2>&1 &
for _ in $(seq 30); do
    grep -q "Network simulator ready" "$RUN/log/ibsim.log" 2>/dev/null && break
    sleep 1
done
grep -q "Network simulator ready" "$RUN/log/ibsim.log" || {
    echo "ibsim did not come up; see $RUN/log/ibsim.log" >&2; exit 1; }

export LD_PRELOAD="$SHIM" IBSIM_SERVER_NAME=127.0.0.1

# Two subnet managers, the way a UFM HA pair runs them: one per SM host
# (gen_netlist.py puts ufm1 and ufm2 on different leaves). Higher priority wins
# the election; the other sits in STANDBY polling the master and takes over
# when it stops answering. Each needs its own cache dir and log, or they
# trample each other's guid2lid files. SIM_HOST picks which simulated HCA the
# process attaches as.
start_sm() {  # name priority
    local name=$1 prio=$2
    mkdir -p "$RUN/cache-$name" "$RUN/dump-$name"
    echo "starting opensm on $name (priority $prio)"
    SIM_HOST="$name" OSM_CACHE_DIR="$RUN/cache-$name" \
        opensm --daemon -p "$prio" --log_file "$RUN/log/opensm-$name.log" \
        --dump_files_dir "$RUN/dump-$name" \
        > "$RUN/log/osm-$name.out" 2>&1
}
start_sm ufm1 14
start_sm ufm2 10

for _ in $(seq 60); do
    grep -q "SUBNET UP" "$RUN/log/opensm-ufm1.log" 2>/dev/null && break
    sleep 2
done

if grep -q "SUBNET UP" "$RUN/log/opensm-ufm1.log" 2>/dev/null; then
    echo "fabric up -- logs in $RUN/log; check roles with ibfabric/sm_status.sh"
else
    echo "opensm on ufm1 did not report SUBNET UP; last lines:" >&2
    tail -n 5 "$RUN/log/opensm-ufm1.log" "$RUN/log/osm-ufm1.out" >&2
    exit 1
fi
