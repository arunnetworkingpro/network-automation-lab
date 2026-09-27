#!/usr/bin/env bash
# Open a shell "on" a simulated SM host: every IB tool run in it acts as that node.
#
#   ~/ufm ufm1                 interactive shell as ufm1 (or ufm2)
#   ~/ufm ufm1 sminfo          run one command as ufm1 and return
#
# Runs from its own work dir: libumad2sim builds a fake sysfs at ./sys-<pid> in
# the CURRENT directory and ibpanics if that is not writable -- and otherwise
# litters wherever you happened to be (the stray sys-NNNN dirs in the repo).
#
# ufm1/ufm2 are not machines -- they are HCAs inside ibsim on the Pi, so there is
# nothing to ssh to. SIM_HOST picks which simulated node the umad2sim shim attaches
# as; ibenv.sh preloads the shim. Type 'exit' to leave.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
host="${1:-ufm1}"
shift || true
work="$HOME/.local/share/ibfabric/work"
mkdir -p "$work" && cd "$work" || exit 1
rm -rf "$work"/sys-* 2>/dev/null

if [ $# -gt 0 ]; then
    source "$HERE/ibenv.sh" >/dev/null 2>&1
    export SIM_HOST=$host PATH="$PATH:/usr/sbin:/sbin"
    "$@" 2> >(grep -v '^ibwarn' >&2)
    rc=$?
    rm -rf "$work"/sys-* 2>/dev/null
    exit $rc
fi
rc="${XDG_RUNTIME_DIR:-/tmp}/ufm-$host.rc"
cat >"$rc" <<RC
[ -f ~/.bashrc ] && source ~/.bashrc
source "$HERE/ibenv.sh" >/dev/null 2>&1
export SIM_HOST=$host
# infiniband-diags installs into /usr/sbin, which a non-login ssh shell lacks
export PATH="\$PATH:/usr/sbin:/sbin:$HERE/bin"
# Lab-only: no passwordless sudo on the Pi, and these tools need no root here.
# 'sudo <cmd>' runs <cmd> directly, so real-world commands paste in unchanged.
sudo() { "\$@"; }
PS1="\\u@${host/ufm/ufm0}:~\\$ "
echo "$(echo ${host/ufm/ufm0}) -- simulated IB subnet manager (ibsim). exit to leave."
RC
exec bash --rcfile "$rc" -i
