#!/usr/bin/env python3
"""Give fabric-only hosts a way out, with the jump box as the NAT gateway.

    python scripts/fabric_nat.py

Fabric hosts sit on RFC1918 VLANs with no route to the home LAN, which is
correct until one of them needs the Ubuntu archive. Rather than bolt a second
NIC onto every host -- which CML will not allow while the lab is running anyway
-- the jump box does what a real DC's jump/bastion does: forwards and
masquerades on behalf of the fabric.

Three pieces, none of which touch the RDMA data path:
  * jump masquerades fabric sources out of its home-LAN NIC
  * each leaf gets a default route pointing at the jump's fabric address
  * fabric hosts get a resolver, since a static address inherits none

Reversible: `--off` drops the masquerade and the default routes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cml.client import load_env  # noqa: E402
from scripts.lab_ssh import Host  # noqa: E402

TOPO = ROOT / "topology" / "dc-fabric.yml"
JUMP_LAN = "192.168.2.50"
FABRIC_CIDR = "10.10.0.0/16"
DNS = "192.168.2.1"
OSPF_PROCESS = 1


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def ios(jump: Host, ip: str, commands: list[str]) -> str:
    pw = load_env()["LAB_DEVICE_PASS"]
    script = "\n".join(["conf t", *commands, "end", "write memory"])
    return jump.run(
        f"printf '%s\\n' {_sq(script)} | sshpass -p {_sq(pw)} ssh -tt "
        f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-o KexAlgorithms=+diffie-hellman-group14-sha1 "
        f"-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAuthentication=no "
        f"arun@{ip} 2>&1 | tail -3", timeout=240, check=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", action="store_true", help="remove NAT and the routes")
    args = ap.parse_args()

    topo = yaml.safe_load(TOPO.read_text())
    jump_fabric = topo["jump_server"]["fabric_ip"].split("/")[0]
    ext_if = "ens3"  # home-LAN NIC on the jump box

    with Host(JUMP_LAN) as jump:
        if "sshpass" not in jump.run("command -v sshpass || true", check=False):
            jump.sudo("DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sshpass",
                      timeout=600)

        rule = (f"-t nat {{action}} POSTROUTING -s {FABRIC_CIDR} "
                f"-o {ext_if} -j MASQUERADE")
        if args.off:
            jump.sudo(f"iptables {rule.format(action='-D')} || true", timeout=60)
            print("jump: masquerade removed")
        else:
            # -C first: iptables happily stacks identical rules.
            jump.sudo(
                f"iptables {rule.format(action='-C')} 2>/dev/null || "
                f"iptables {rule.format(action='-A')}", timeout=60)
            jump.sudo("sysctl -w net.ipv4.ip_forward=1 >/dev/null", timeout=60)
            print(f"jump: masquerading {FABRIC_CIDR} out of {ext_if}")

        # The default goes on the jump server's own leaf only, and OSPF carries
        # it to everyone else. A static default on every leaf looks equivalent
        # and is not: a recursive static does not tunnel, so each hop routes the
        # packet on its own. leaf2 would hand 1.1.1.1 to a spine, and the spine
        # -- having no default of its own -- answers Destination Unreachable.
        verb = "no " if args.off else ""
        gw_leaf = topo["jump_server"]["leaf"]
        gw_loopback = next(l["loopback"] for l in topo["leaves"]
                           if l["name"] == gw_leaf)
        ios(jump, gw_loopback, [
            f"{verb}ip route 0.0.0.0 0.0.0.0 {jump_fabric}",
            f"router ospf {OSPF_PROCESS}",
            f" {verb}default-information originate",
        ])
        print(f"{gw_leaf}: default {'removed' if args.off else '-> ' + jump_fabric}"
              f" and {'withdrawn from' if args.off else 'originated into'} OSPF")

        # Clean up the per-leaf statics an earlier version of this script left.
        for leaf in topo["leaves"]:
            if leaf["name"] != gw_leaf:
                ios(jump, leaf["loopback"],
                    [f"no ip route 0.0.0.0 0.0.0.0 {jump_fabric}"])

        if not args.off:
            print(f"\nfabric hosts still need a resolver: nameserver {DNS}")


if __name__ == "__main__":
    main()
