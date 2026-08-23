# Source this before running any IB tool against the simulated fabric:
#
#     source ibfabric/ibenv.sh
#     ibnetdiscover | head
#     iblinkinfo
#
# Without the preload the tools talk to /dev/infiniband, find nothing, and
# report "No IB devices found" -- which looks like a broken fabric rather than
# a missing shim.
#
# Attaching a CML Linux host to the Pi's ibsim over the network DOES NOT WORK.
# ibsim's remote mode (-r) accepts a local client over the LAN IP fine, but a
# client on another host dies in sim_init with "ctl failed(read)" / "No route to
# host". Ruled out, in order: UDP reachability both directions (verified with a
# listener), a client/server version skew (rebuilt 0.12 from source on the host
# to match), and stale client registrations (clean fabric restart). Don't redo
# that work -- it is something inside ibsim's ctl exchange.
#
# What works instead: run a second ibsim on the CML host itself, against a copy
# of the same netlist, and attach over the local unix socket:
#
#     ibsim -s -n /tmp/fat-tree.net &
#     export LD_PRELOAD=... SIM_HOST=jump   # no IBSIM_SERVER_NAME
#
# Each host then gets its own view of the same topology. Fine for learning the
# tools; it is not one shared fabric.

export LD_PRELOAD="$(find /usr/lib -name libumad2sim.so -print -quit 2>/dev/null)"
export IBSIM_SERVER_NAME="${IBSIM_SERVER_NAME:-127.0.0.1}"

[ -n "$LD_PRELOAD" ] || echo "warning: libumad2sim.so not found" >&2
