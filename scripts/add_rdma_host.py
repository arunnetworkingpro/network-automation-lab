#!/usr/bin/env python3
"""Add an RDMA host from topology/dc-fabric.yml to the *running* lab.

    python scripts/add_rdma_host.py rdma1

Deliberately not part of build_topology.py: that script builds a lab from
nothing and refuses to touch an existing one, and rebuilding the fabric to gain
one host would throw away every switch config and boot the whole thing again.
This adds a node the way you would add a server to a real rack -- cable it into
a free port, put the port in the right VLAN, boot it.

What it does:
  1. creates the Ubuntu node and cables it to a free port on its leaf
  2. renders cloud-init from templates/rdma.j2 (one NIC, fabric side only)
  3. boots it, then puts the leaf port into the access VLAN over SSH via jump

One NIC, not two. A second NIC on the external connector would be the obvious
way to give the host internet, but the connector cannot grow a port while the
lab is running and CML locks physical configuration lab-wide -- so it would cost
a full fabric shutdown. Run scripts/fabric_nat.py instead: the jump box then
NATs for the whole fabric, which is what a real bastion does anyway.

Leaf configuration goes through the jump box because the Pi has no route into
10.0.0.0/8; that is the same path Ansible uses. Reach the host itself the same
way: Host("10.10.20.12", via="192.168.2.50").
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cml.client import connect, load_env  # noqa: E402
from scripts.lab_ssh import Host  # noqa: E402

TOPO = ROOT / "topology" / "dc-fabric.yml"
DNS = "192.168.2.1"
BOOT_TIMEOUT = 420


def _leaf_loopback(topo: dict, leaf_name: str) -> str:
    for leaf in topo["leaves"]:
        if leaf["name"] == leaf_name:
            return leaf["loopback"]
    sys.exit(f"{leaf_name} is not in the topology")


def _vlan_gateway(topo: dict, vlan_id: int) -> str:
    for v in topo["vlans"]:
        if v["id"] == vlan_id:
            return v["gateway"]
    sys.exit(f"vlan {vlan_id} is not in the topology")


def render_cloud_init(topo: dict, spec: dict, fab_if: str) -> str:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      trim_blocks=True, lstrip_blocks=True)
    return env.get_template("rdma.j2").render(
        name=spec["name"],
        user=load_env().get("LAB_USER", "arun"),
        password=load_env()["LAB_DEVICE_PASS"],
        ssh_key=load_env().get("LAB_ADMIN_SSH_PUBKEY") or None,
        fab_if=fab_if,
        fab_ip=spec["fabric_ip"],
        fab_vlan=spec["vlan"],
        fab_peer=spec["leaf"],
        fab_gateway=_vlan_gateway(topo, spec["vlan"]),
        dns=DNS,
    )


def configure_leaf_port(topo: dict, spec: dict, port: str) -> None:
    """Put the newly cabled leaf port into the host's access VLAN."""
    leaf_ip = _leaf_loopback(topo, spec["leaf"])
    pw = load_env()["LAB_DEVICE_PASS"]
    cmds = "\n".join([
        "conf t",
        f"interface {port}",
        " switchport mode access",
        f" switchport access vlan {spec['vlan']}",
        f" description {spec['name']} (RDMA host)",
        " no shutdown",
        "end", "write memory",
    ])
    with Host("192.168.2.50") as jump:
        if "sshpass" not in jump.run("command -v sshpass || true", check=False):
            jump.sudo("DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sshpass",
                      timeout=600)
        out = jump.run(
            f"printf '%s\\n' {_sq(cmds)} | sshpass -p {_sq(pw)} ssh -tt "
            f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o KexAlgorithms=+diffie-hellman-group14-sha1 "
            f"-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAuthentication=no "
            f"arun@{leaf_ip} 2>&1 | tail -5",
            timeout=180, check=False)
    print(f"  leaf {spec['leaf']} {port} -> access vlan {spec['vlan']}")
    if "Invalid" in out or "denied" in out.lower():
        print(f"  [warn] leaf said: {out.strip()[:200]}")


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def free_interface(node):
    """A spare physical port on an already-running node.

    CML locks a node's physical configuration while it is BOOTED, so
    create_interface() on a live switch fails with "Physical configuration of
    node is locked". Nodes are built with spare ports though, so cabling into an
    existing unused one avoids taking the leaf down -- which is also how you
    would add a server to a live rack.
    """
    for i in node.interfaces():
        if not i.connected and not i.label.lower().startswith("loopback"):
            return i
    if node.state == "BOOTED":
        sys.exit(f"{node.label} has no free port and is running -- stop it first")
    return node.create_interface()


def external_port(ext):
    """A free port on the external connector, stopping it briefly if need be.

    The connector ships with exactly the ports it was built with, and unlike a
    switch it cannot grow one while running. Stopping it drops the bridged link
    for a few seconds -- the jump box loses its home-LAN NIC and gets it back
    when the connector restarts, keeping its netplan address. Cheap, but not
    silent, hence the warning.
    """
    for i in ext.interfaces():
        if not i.connected:
            return i
    print("  ext has no free port -- restarting it (bridged link drops briefly)")
    ext.stop()
    for _ in range(30):
        if ext.state != "BOOTED":
            break
        time.sleep(2)
    try:
        port = ext.create_interface()
    finally:
        # Without the finally, a failure here leaves the connector stopped and
        # the jump box cut off from the home LAN -- which also cuts off the SSH
        # path this script needs to finish. Learned the hard way.
        ext.start()
        for _ in range(30):
            if ext.state == "BOOTED":
                break
            time.sleep(2)
    print(f"  ext back up ({ext.state}) with {port.label}")
    return port


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--skip-leaf-config", action="store_true")
    args = ap.parse_args()

    topo = yaml.safe_load(TOPO.read_text())
    spec = next((h for h in topo.get("rdma_hosts", []) if h["name"] == args.name), None)
    if spec is None:
        sys.exit(f"{args.name} is not under rdma_hosts: in {TOPO.name}")

    client = connect()
    lab = next((l for l in client.all_labs() if l.title == topo["lab"]["title"]), None)
    if lab is None:
        sys.exit(f"lab {topo['lab']['title']!r} is not on the controller")

    lab.sync()
    by_name = {n.label: n for n in lab.nodes()}
    if spec["leaf"] not in by_name:
        sys.exit(f"expected node {spec['leaf']!r} in the lab, not found")

    # Resumable: a half-built node from an earlier failed run is picked up
    # rather than left orphaned in the lab.
    node = by_name.get(spec["name"])
    if node is None:
        node = lab.create_node(spec["name"], topo["node_defs"]["jump"], x=400, y=400)
    elif node.state == "BOOTED":
        sys.exit(f"{spec['name']} is already running -- nothing to do")

    # A resumed run finds the cable already in place; a fresh one lays it.
    cabled = next((i for i in node.interfaces() if i.connected), None)
    if cabled is not None:
        fab_if = cabled
        leaf_if = next(x for x in (cabled.link.interface_a, cabled.link.interface_b)
                       if x != cabled)
    else:
        fab_if = free_interface(node)
        leaf_if = free_interface(by_name[spec["leaf"]])
        lab.create_link(fab_if, leaf_if)

    node.configuration = render_cloud_init(topo, spec, fab_if.label)
    print(f"created {spec['name']}: {fab_if.label} -> {spec['leaf']}:{leaf_if.label} "
          f"({spec['fabric_ip']})")

    node.start()
    print("booting", end="", flush=True)
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if node.state == "BOOTED":
            break
        print(".", end="", flush=True)
        time.sleep(10)
    print(f" {node.state}")

    if not args.skip_leaf_config:
        configure_leaf_port(topo, spec, leaf_if.label)


if __name__ == "__main__":
    main()
