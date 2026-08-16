#!/usr/bin/env python3
"""Generate the Ansible inventory from the same source of truth as everything else.

A hand-written inventory is a second place to describe the fabric, and second places
drift. This reads topology/dc-fabric.yml and topology/link-map.yml and writes
ansible/inventory/generated_fabric.yml -- which is gitignored, because it is an
artifact, not a document.

Two things it encodes that are easy to get wrong by hand:

  * Every switch is reached through the jump box. The Pi has no route into
    10.0.0.0/8 until the home router learns one, so each host gets a ProxyCommand.
  * No password appears in the file. ansible_password resolves from the environment
    at run time, so the inventory stays safe to read, print, and paste.

    python scripts/gen_inventory.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from rich.console import Console

console = Console()

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY = ROOT / "topology" / "dc-fabric.yml"
OUT = ROOT / "ansible" / "inventory" / "generated_fabric.yml"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--onjump",
        action="store_true",
        help="inventory for running Ansible ON the jump box: no ProxyCommand, since "
        "the fabric is directly reachable from there",
    )
    args = ap.parse_args()

    topo = yaml.safe_load(TOPOLOGY.read_text())
    jump = topo["jump_server"]
    jump_ip = jump["external_ip"].split("/")[0]

    # -W %h:%p turns ssh into a plain TCP forwarder -- no shell on the jump, just a
    # pipe to the switch. BatchMode makes a missing key fail fast instead of hanging
    # on a password prompt inside an Ansible task.
    #
    # Only needed when running from outside. CML's external connector is 'protected'
    # and 'snooped', which drops frames whose source or destination is not the jump's
    # own address -- so the Pi cannot route through the jump into 10.0.0.0/8 no matter
    # what static routes exist. Running Ansible on the jump sidesteps that entirely,
    # and is what the jump box is for.
    proxy = (
        ""
        if args.onjump
        else f"-o ProxyCommand=\"ssh -W %h:%p -q -o BatchMode=yes "
        f"-o StrictHostKeyChecking=accept-new arun@{jump_ip}\""
    )

    def host_entry(dev: dict) -> dict:
        return {"ansible_host": dev["loopback"]}

    inventory = {
        "all": {
            "children": {
                "fabric": {
                    "children": {
                        "spines": {
                            "hosts": {d["name"]: host_entry(d) for d in topo["spines"]}
                        },
                        "leaves": {
                            "hosts": {d["name"]: host_entry(d) for d in topo["leaves"]}
                        },
                    },
                    "vars": {
                        "ansible_connection": "ansible.netcommon.network_cli",
                        "ansible_network_os": "cisco.ios.ios",
                        "ansible_user": "arun",
                        # Never written to disk. Export LAB_DEVICE_PASS before running;
                        # it lives in ~/.cml.env, chmod 600.
                        "ansible_password": "{{ lookup('env', 'LAB_DEVICE_PASS') }}",
                        "ansible_become": True,
                        "ansible_become_method": "ansible.netcommon.enable",
                        "ansible_become_password": "{{ lookup('env', 'LAB_DEVICE_PASS') }}",
                        **({} if args.onjump else {"ansible_ssh_common_args": proxy}),
                        # IOL is slow to answer under load; the default 30s times out
                        # on a first connection while the box is still settling.
                        "ansible_command_timeout": 90,
                        "ansible_connect_timeout": 60,
                    },
                },
                # Group name deliberately differs from the host name inside it --
                # Ansible warns and behaves oddly when a group and a host share one.
                "jumphosts": {
                    "hosts": {
                        jump["name"]: {
                            "ansible_host": jump_ip,
                            "ansible_connection": "ssh",
                            "ansible_user": "arun",
                            "ansible_python_interpreter": "/usr/bin/python3",
                        }
                    }
                },
            },
            "vars": {
                # BGP AS and the underlay facts, so playbooks can assert against the
                # design rather than against hardcoded numbers.
                "bgp_asn": topo["underlay"]["bgp_asn"],
                "ospf_area": topo["underlay"]["ospf_area"],
                "spine_loopbacks": [s["loopback"] for s in topo["spines"]],
                "leaf_loopbacks": [l["loopback"] for l in topo["leaves"]],
                "expected_ospf_neighbors": len(topo["leaves"]),
            },
        }
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "# GENERATED by scripts/gen_inventory.py -- do not hand-edit, it is gitignored.\n"
        "# Regenerate after changing topology/dc-fabric.yml.\n"
        + yaml.safe_dump(inventory, sort_keys=False, default_flow_style=False)
    )

    n = len(topo["spines"]) + len(topo["leaves"])
    console.print(f"wrote [cyan]{OUT.relative_to(ROOT)}[/cyan]  ({n} switches + jump)")
    console.print("[dim]run with: export LAB_DEVICE_PASS=$(grep '^LAB_DEVICE_PASS=' "
                  "~/.cml.env | cut -d= -f2-)")


if __name__ == "__main__":
    main()
