#!/usr/bin/env bash
# Open a shell "on" a simulated SM host: every IB tool run in it acts as that node.
#
#   ibfabric/ufm.sh ufm1      (or ufm2; ~/ufm is a symlink to this)
#
# ufm1/ufm2 are not machines -- they are HCAs inside ibsim on the Pi, so there is
# nothing to ssh to. SIM_HOST picks which simulated node the umad2sim shim attaches
# as; ibenv.sh preloads the shim. Type 'exit' to leave.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
host="${1:-ufm1}"
rc="${XDG_RUNTIME_DIR:-/tmp}/ufm-$host.rc"
cat >"$rc" <<RC
[ -f ~/.bashrc ] && source ~/.bashrc
source "$HERE/ibenv.sh" >/dev/null 2>&1
export SIM_HOST=$host
PS1="[$host \\W]\\$ "
echo "on $host (simulated) -- try: ibstat, sminfo, ibhosts, iblinkinfo | less; exit to leave"
RC
exec bash --rcfile "$rc" -i
