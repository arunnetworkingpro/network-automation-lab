#!/usr/bin/env python3
"""Add a server from topology/dc-fabric.yml to the *running* lab, or pin MACs.

    python scripts/add_server.py srv-app2          # create, cable, boot
    python scripts/add_server.py --pin-mac         # fix MACs on existing servers

Same reasoning as add_rdma_host.py: rebuilding the fabric to gain one host would
throw away every switch config, so this cables into a free leaf port instead.

Every server's eth0 gets a MAC derived from its IP (cml/hosts.py), because the
leaves hand out addresses by manual DHCP binding on that MAC -- with an anycast
gateway, a plain pool on each leaf would race. CML only accepts a MAC on a node
that is wiped, so --pin-mac stops and wipes each server whose MAC is wrong, sets
it, and starts it again; alpine keeps nothing on disk and boots in seconds.

The leaf side (access port, DHCP binding) is not configured here. That is
gen_configs.py's job -- this writes the new cable into topology/link-map.yml, so
the next render includes the port on the leaf and the binding on every leaf with
the VLAN's SVI. Render and push the leaves afterwards.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from netaddr import IPNetwork

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cml.client import connect, load_env  # noqa: E402
from cml.hosts import host_mac  # noqa: E402
from scripts.add_rdma_host import free_interface  # noqa: E402

TOPO = ROOT / "topology" / "dc-fabric.yml"
LINK_MAP = ROOT / "topology" / "link-map.yml"
BOOT_TIMEOUT = 300
# Servers that must come back up even if found stopped (e.g. by an interrupted run).
RESTART = {"srv-app1", "srv-db1", "srv-app2"}


def render_server(topo: dict, spec: dict) -> str:
    vlan = next(v for v in topo["vlans"] if v["id"] == spec["vlan"])
    env = Environment(loader=FileSystemLoader(ROOT / "templates"),
                      trim_blocks=True, lstrip_blocks=True)
    ip = IPNetwork(spec["ip"])
    return env.get_template("server.j2").render(
        name=spec["name"], user="arun", password=load_env()["LAB_DEVICE_PASS"],
        iface="eth0", ip=str(ip.ip), prefix=ip.prefixlen, netmask=str(ip.netmask),
        gateway=vlan["gateway"], vlan=spec["vlan"], leaf=spec["leaf"],
    )


def wait_booted(node) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline and node.state != "BOOTED":
        time.sleep(5)
    print(f"  {node.label}: {node.state}")


def eth0(node):
    return next(i for i in node.interfaces() if i.label == "eth0")


def pin_macs(lab, topo: dict) -> None:
    for spec in topo["servers"]:
        node = next((n for n in lab.nodes() if n.label == spec["name"]), None)
        if node is None:
            print(f"  {spec['name']}: not in the lab yet -- run add_server.py {spec['name']}")
            continue
        want = host_mac(spec["ip"])
        iface = eth0(node)
        if (iface.mac_address or "").lower() == want:
            print(f"  {spec['name']}: {want} already pinned")
            continue
        running = node.state in ("STARTED", "BOOTED") or spec["name"] in RESTART
        # Stopped is not enough: CML keeps the physical configuration locked
        # until the node is wiped. Alpine keeps nothing worth saving on disk.
        if node.state != "DEFINED_ON_CORE":
            node.stop(wait=True)
            node.wipe(wait=True)
        iface.mac_address = want
        print(f"  {spec['name']}: eth0 -> {want}")
        if running:
            node.start(wait=False)
            wait_booted(node)


def record_link(spec: dict, srv_if: str, leaf_if: str) -> None:
    lm = yaml.safe_load(LINK_MAP.read_text())
    if any(l["a"]["node"] == spec["name"] for l in lm["links"]):
        return
    lm["links"].append({
        "a": {"node": spec["name"], "interface": srv_if, "ip": spec["ip"]},
        "b": {"node": spec["leaf"], "interface": leaf_if, "access_vlan": spec["vlan"]},
    })
    header = "".join(l for l in LINK_MAP.read_text().splitlines(keepends=True)
                     if l.startswith("#"))
    LINK_MAP.write_text(header + yaml.safe_dump(lm, sort_keys=False))
    print(f"  recorded {spec['name']}:{srv_if} <-> {spec['leaf']}:{leaf_if} in link-map.yml")


def add(lab, topo: dict, name: str) -> None:
    spec = next((s for s in topo["servers"] if s["name"] == name), None)
    if spec is None:
        sys.exit(f"{name} is not under servers: in {TOPO.name}")
    by_name = {n.label: n for n in lab.nodes()}

    node = by_name.get(name)
    if node is None:
        node = lab.create_node(name, topo["node_defs"]["server"], x=600, y=400)
        print(f"  created {name}")
    cabled = next((i for i in node.interfaces() if i.connected), None)
    if cabled is None:
        srv_if = eth0(node) if any(i.label == "eth0" for i in node.interfaces()) \
            else node.create_interface()
        leaf_if = free_interface(by_name[spec["leaf"]])
        lab.create_link(srv_if, leaf_if)
    else:
        srv_if = cabled
        leaf_if = next(x for x in (cabled.link.interface_a, cabled.link.interface_b)
                       if x != cabled)
    record_link(spec, srv_if.label, leaf_if.label)

    if node.state not in ("STARTED", "BOOTED"):
        srv_if.mac_address = host_mac(spec["ip"])
        node.configuration = render_server(topo, spec)
        node.start(wait=False)
        wait_booted(node)
    print(f"  {name}: {srv_if.label} ({host_mac(spec['ip'])}) -> "
          f"{spec['leaf']}:{leaf_if.label}, vlan {spec['vlan']}")
    print("  next: gen_configs.py --push --only <leaves>, so the port and the "
          "DHCP binding exist")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--pin-mac", action="store_true")
    args = ap.parse_args()
    if not args.name and not args.pin_mac:
        ap.error("give a server name, or --pin-mac")

    topo = yaml.safe_load(TOPO.read_text())
    lab = next((l for l in connect().all_labs() if l.title == topo["lab"]["title"]), None)
    if lab is None:
        sys.exit(f"lab {topo['lab']['title']!r} is not on the controller")
    lab.sync()
    if args.name:
        add(lab, topo, args.name)
    if args.pin_mac:
        pin_macs(lab, topo)


if __name__ == "__main__":
    main()
