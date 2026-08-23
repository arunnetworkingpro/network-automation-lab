#!/usr/bin/env python3
"""Turn a CML Linux host into a Soft-RoCE (RoCEv2) endpoint.

    python3 ibfabric/setup_roce.py 192.168.2.50 --netdev ens2

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
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.lab_ssh import Host  # noqa: E402

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


def setup(address: str, netdev: str, name: str, via: str | None = None) -> None:
    with Host(address, via=via) as h:
        host = h.run("hostname")
        print(f"[{host}] kernel {h.run('uname -r')}")

        if "rdma_rxe" not in h.run("modinfo -F filename rdma_rxe 2>&1", check=False):
            sys.exit(f"[{host}] rdma_rxe not available -- install "
                     f"linux-modules-extra-$(uname -r) and reboot")

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--netdev", default="ens2",
                    help="fabric-facing NIC to attach RoCE to (default ens2)")
    ap.add_argument("--name", default="rxe0")
    ap.add_argument("--via", default=None,
                    help="jump host, for fabric-only nodes with no LAN route")
    args = ap.parse_args()
    setup(args.address, args.netdev, args.name, args.via)


if __name__ == "__main__":
    main()
