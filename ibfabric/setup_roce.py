#!/usr/bin/env python3
"""Turn a CML Linux host into a Soft-RoCE (RoCEv2) endpoint.

    python3 ibfabric/setup_roce.py 192.168.2.50 --netdev ens2
    python3 ibfabric/setup_roce.py 10.10.20.12 --netdev ens2 --via 192.168.2.50

This is the half of the RDMA lab that is genuinely real: rdma_rxe implements
RoCEv2 in the kernel over an ordinary Ethernet NIC, so verbs, queue pairs and
`ib_send_bw` all behave for real -- over the actual spine-leaf fabric. What it
cannot give you is lossless behaviour: PFC, ECN and DCQCN need switch silicon,
and IOL will accept that config while ignoring it.

Attach to the *fabric-facing* NIC (ens2, towards leaf1), not the management one.
RDMA on the management interface would bypass the topology and measure nothing
interesting.

Soft-RoCE is CPU-bound -- every packet is built in software. On a 6-core compute
already oversubscribed 2x, two hosts benchmarking at once is the honest limit.

Root cause of the long-parked "Soft-RoCE down" state, found by actually running
this against a live lab rather than assuming the module was just missing:
rdma_rxe ships in linux-modules-extra, a package keyed to one *exact* kernel
build, not the kernel major version. jump's running kernel had drifted to an
orphaned point release with no matching package left in the archive at all --
`apt install linux-modules-extra-$(uname -r)` 404s, full stop, no amount of
retrying fixes that. ensure_kernel_modules() below detects exactly that case and
resolves it the only way that works: `apt install linux-generic` (pulls a
currently-archived, matched kernel+modules pair) and a reboot onto it. rdma1
happened to already be on a kernel the archive still has, so it never needed
the reboot path -- both cases are handled rather than assumed.

The other apparent failure -- ib_send_bw printing nothing and eventually dying
under a hard `timeout` -- wasn't a network hang either: a tcpdump during a
"stuck" run showed real RoCEv2 traffic (UDP/4791) crossing the fabric the whole
time. It buffers all output until the run completes, so an outer timeout
shorter than its default iteration count just discards the results before they
print. bw_server()/bw_test() below use its own -D duration flag instead.

rdma1 has no internet by itself (see templates/rdma.j2) -- run
scripts/fabric_nat.py once first, and pass --via so this script's own apt calls
have a route out.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.lab_ssh import CommandFailed, Host  # noqa: E402

# rdma link add is not persistent, and neither is the module load. Both have to
# be re-done at boot or the host comes back as a plain Ethernet box.
UNIT = """[Unit]
Description=Soft-RoCE device on %(netdev)s
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/modprobe rdma_rxe
ExecStart=/bin/sh -c '/usr/sbin/rdma link show %(name)s >/dev/null 2>&1 || \
/usr/sbin/rdma link add %(name)s type rxe netdev %(netdev)s'

[Install]
WantedBy=multi-user.target
"""

REBOOT_TIMEOUT = 180


def ensure_kernel_modules(h: Host, via: str | None) -> Host:
    """Make sure linux-modules-extra for the *running* kernel is installed.

    Rebuilds and returns the Host if a reboot onto a new kernel was needed --
    the caller's `h` is stale (connection closed) once this returns a new one.
    """
    kernel = h.run("uname -r")
    have = h.run(
        f"dpkg -s linux-modules-extra-{kernel} >/dev/null 2>&1 && echo yes || echo no",
        check=False,
    )
    if have == "yes":
        return h

    try:
        h.sudo(f"apt-get install -y -qq linux-modules-extra-{kernel}", timeout=180)
        return h
    except CommandFailed:
        pass

    print(f"  [{h.address}] no linux-modules-extra-{kernel} in the archive "
          "(orphaned point release) -- moving to a current kernel")
    h.sudo("apt-get update -qq", timeout=120)
    h.sudo("apt-get install -y -qq linux-generic", timeout=300)
    print(f"  [{h.address}] rebooting...")
    try:
        h.sudo("reboot", timeout=5)
    except Exception:
        pass  # the reboot itself kills the channel before it can reply cleanly
    h.__exit__()

    time.sleep(15)
    deadline = time.time() + REBOOT_TIMEOUT
    while time.time() < deadline:
        try:
            fresh = Host(h.address, via=via).__enter__()
            new_kernel = fresh.run("uname -r")
            print(f"  [{h.address}] back up on {new_kernel}")
            return fresh
        except Exception:
            time.sleep(5)
    sys.exit(f"[{h.address}] did not come back within {REBOOT_TIMEOUT}s of rebooting")


def setup(address: str, netdev: str, name: str, via: str | None = None) -> Host:
    """Bring up Soft-RoCE on one host. Returns the (possibly reconnected) Host,
    left open, so callers can chain a bw test onto the same connection."""
    h = Host(address, via=via).__enter__()
    host = h.run("hostname")
    print(f"[{host}] kernel {h.run('uname -r')}")

    h = ensure_kernel_modules(h, via)
    host = h.run("hostname")

    h.sudo("apt-get install -y -qq rdma-core ibverbs-utils perftest", timeout=180)

    h.sudo("modprobe rdma_rxe", timeout=60)
    existing = h.run("rdma link show 2>/dev/null", check=False)
    if name not in existing:
        h.sudo(f"rdma link add {name} type rxe netdev {netdev}", timeout=60)
        print(f"[{host}] created {name} on {netdev}")
    else:
        print(f"[{host}] {name} already present")

    h.sudo("tee /etc/modules-load.d/rdma_rxe.conf >/dev/null <<'EOF'\n"
           "rdma_rxe\nEOF", timeout=60)
    h.sudo("tee /etc/systemd/system/rxe-setup.service >/dev/null <<'EOF'\n"
           + UNIT % {"netdev": netdev, "name": name} + "EOF", timeout=60)
    h.sudo("systemctl daemon-reload && systemctl enable --now rxe-setup",
           timeout=120)

    print(f"[{host}] rdma link: {h.run('rdma link show', check=False)}")
    state = h.run(f"ibv_devinfo -d {name} 2>&1 | "
                  "grep -E 'state|link_layer|transport|fw_ver'", check=False)
    print(f"[{host}] {state}")
    return h


def bw_server(h: Host, duration: int) -> None:
    """Start ib_send_bw detached and waiting for one client, then return."""
    h.run(
        f"pkill ib_send_bw 2>/dev/null; sleep 1; "
        f"setsid nohup ib_send_bw -D {duration} < /dev/null "
        "> /tmp/ib_send_bw_server.log 2>&1 & disown; sleep 1; echo started",
        check=False,
    )
    print(f"[{h.run('hostname')}] ib_send_bw server running ({duration}s window)")


def bw_test(h: Host, peer: str, duration: int) -> None:
    """Run the ib_send_bw client against a server already started on `peer`."""
    out = h.run(f"timeout {duration + 15} ib_send_bw -D {duration} {peer} 2>&1",
                timeout=duration + 20, check=False)
    print(f"[{h.run('hostname')} -> {peer}] ib_send_bw result:")
    print(out or "(no output)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--netdev", default="ens2",
                    help="fabric-facing NIC to attach RoCE to (default ens2)")
    ap.add_argument("--name", default="rxe0")
    ap.add_argument("--via", default=None,
                    help="jump host, for fabric-only nodes with no LAN route")
    ap.add_argument("--bw-server", type=int, metavar="SECONDS",
                    help="after setup, start ib_send_bw waiting for one client "
                         "for SECONDS and exit")
    ap.add_argument("--bw-test", metavar="PEER_IP",
                    help="after setup, run the ib_send_bw client against a "
                         "server already started on PEER_IP")
    ap.add_argument("--bw-duration", type=int, default=5)
    args = ap.parse_args()

    h = setup(args.address, args.netdev, args.name, args.via)
    if args.bw_server:
        bw_server(h, args.bw_server)
    if args.bw_test:
        bw_test(h, args.bw_test, args.bw_duration)
    h.__exit__()


if __name__ == "__main__":
    main()
