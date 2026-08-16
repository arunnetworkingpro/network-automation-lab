#!/usr/bin/env python3
"""Build the Phase 1 lab in CML from topology/dc-fabric.yml.

Nothing here is hand-placed or hand-cabled: nodes, interfaces, links and the /31
addressing are all derived from the YAML. Re-running against the same title is
refused unless you pass --replace, so a stray second run can't quietly leave you
with two half-built fabrics.

The script writes topology/link-map.yml on the way out -- the record of which
physical interface ended up carrying which /31. Config generation reads that,
rather than assuming Ethernet0/1 means what we hoped it meant.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from rich.console import Console

from cml.client import connect

console = Console()

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY = ROOT / "topology" / "dc-fabric.yml"
LINK_MAP = ROOT / "topology" / "link-map.yml"

# Grid spacing for the CML canvas. Purely cosmetic, but a fabric you can read at a
# glance is worth the twenty lines it costs.
COL = 220
ROW = 180


def p2p(spine_idx: int, leaf_idx: int) -> tuple[str, str, str]:
    """Return (spine_ip, leaf_ip, prefix) for one spine-leaf link.

    Scheme: 10.1.<spine>.<leaf*2>/31 -- spine takes the even address, leaf the odd.
    Readable on sight: 10.1.2.5 is 'spine2, leaf3 side'. Deterministic, so the same
    link gets the same /31 on every rebuild.
    """
    third = spine_idx + 1
    fourth = leaf_idx * 2
    return (f"10.1.{third}.{fourth}", f"10.1.{third}.{fourth + 1}", "31")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--replace",
        action="store_true",
        help="wipe an existing lab of the same title first",
    )
    ap.add_argument(
        "--start", action="store_true", help="start the lab once it is built"
    )
    args = ap.parse_args()

    topo = yaml.safe_load(TOPOLOGY.read_text())
    title = topo["lab"]["title"]
    defs = topo["node_defs"]

    client = connect()

    # --- refuse to trample an existing lab ---------------------------------
    existing = [lab for lab in client.all_labs(show_all=True) if lab.title == title]
    if existing:
        if not args.replace:
            sys.exit(
                f"A lab titled {title!r} already exists ({existing[0].id}). "
                "Pass --replace to wipe and rebuild it."
            )
        for lab in existing:
            console.print(f"[yellow]removing existing lab {lab.id}")
            lab.stop(wait=True)
            lab.wipe(wait=True)
            lab.remove()

    lab = client.create_lab(title=title)
    lab.description = topo["lab"]["description"]
    console.print(f"[green]created lab[/green] {title}  id={lab.id}")

    nodes: dict[str, object] = {}

    # --- spines: one row across the top ------------------------------------
    spines = topo["spines"]
    for i, spine in enumerate(spines):
        n = lab.create_node(spine["name"], defs["spine"], x=COL * (i * 2 + 1), y=0)
        nodes[spine["name"]] = n
        console.print(f"  spine  {spine['name']:8} lo0={spine['loopback']}")

    # --- leaves: one row beneath -------------------------------------------
    leaves = topo["leaves"]
    for i, leaf in enumerate(leaves):
        n = lab.create_node(leaf["name"], defs["leaf"], x=COL * i, y=ROW)
        nodes[leaf["name"]] = n
        console.print(f"  leaf   {leaf['name']:8} lo0={leaf['loopback']}")

    # --- the mesh ----------------------------------------------------------
    # Every leaf to every spine. Interface order is deterministic because we walk
    # spines in the outer loop: leaf1's first uplink is always to spine1.
    link_map = []
    for si, spine in enumerate(spines):
        for li, leaf in enumerate(leaves):
            spine_ip, leaf_ip, plen = p2p(si, li)
            si_if = nodes[spine["name"]].create_interface()
            li_if = nodes[leaf["name"]].create_interface()
            lab.create_link(si_if, li_if)
            link_map.append(
                {
                    "a": {
                        "node": spine["name"],
                        "interface": si_if.label,
                        "ip": f"{spine_ip}/{plen}",
                    },
                    "b": {
                        "node": leaf["name"],
                        "interface": li_if.label,
                        "ip": f"{leaf_ip}/{plen}",
                    },
                }
            )
            console.print(
                f"  link   {spine['name']}:{si_if.label} <-> "
                f"{leaf['name']}:{li_if.label}   {spine_ip}/{plen}"
            )

    # --- servers, hung off their leaves ------------------------------------
    for i, srv in enumerate(topo["servers"]):
        n = lab.create_node(srv["name"], defs["server"], x=COL * i, y=ROW * 2)
        nodes[srv["name"]] = n
        leaf_node = nodes[srv["leaf"]]
        s_if = n.create_interface()
        l_if = leaf_node.create_interface()
        lab.create_link(s_if, l_if)
        link_map.append(
            {
                "a": {"node": srv["name"], "interface": s_if.label, "ip": srv["ip"]},
                "b": {
                    "node": srv["leaf"],
                    "interface": l_if.label,
                    "access_vlan": srv["vlan"],
                },
            }
        )
        console.print(
            f"  server {srv['name']:9} -> {srv['leaf']}:{l_if.label} "
            f"vlan {srv['vlan']}  {srv['ip']}"
        )

    # --- jump server, fabric side only -------------------------------------
    # Skipped entirely while enabled: false -- see the note in the YAML. The second
    # NIC (external connector -> home LAN) is deliberately NOT built here either.
    # Bridged vs NAT is a decision about your actual home network, not the lab, so
    # it stays out until it's made. See docs/design.md.
    jump = topo["jump_server"]
    if not jump.get("enabled", True):
        console.print(f"[dim]  jump   skipped (enabled: false)")
        _finish(lab, link_map, args)
        return

    jn = lab.create_node(jump["name"], defs["jump"], x=COL * len(leaves), y=ROW * 2)
    nodes[jump["name"]] = jn
    j_if = jn.create_interface()
    jl_if = nodes[jump["leaf"]].create_interface()
    lab.create_link(j_if, jl_if)
    link_map.append(
        {
            "a": {
                "node": jump["name"],
                "interface": j_if.label,
                "ip": jump["fabric_ip"],
            },
            "b": {
                "node": jump["leaf"],
                "interface": jl_if.label,
                "access_vlan": jump["vlan"],
            },
        }
    )
    console.print(
        f"  jump   {jump['name']:9} -> {jump['leaf']}:{jl_if.label} "
        f"vlan {jump['vlan']}  {jump['fabric_ip']}"
    )

    # --- second NIC: out to the real world ---------------------------------
    # The external connector is a node like any other; what makes it bridged rather
    # than NAT is its `configuration`, which must match a connector label the
    # controller actually advertises ("System Bridge" / "NAT"). Set the wrong
    # string and the node builds fine but silently connects to nothing.
    if jump.get("external"):
        ext = lab.create_node(
            "ext", defs["external"], x=COL * len(leaves), y=-ROW
        )
        ext.configuration = jump["external_connector"]
        e_if = ext.create_interface()
        je_if = jn.create_interface()
        lab.create_link(je_if, e_if)
        link_map.append(
            {
                "a": {
                    "node": jump["name"],
                    "interface": je_if.label,
                    "ip": jump["external_ip"],
                    "gateway": jump["external_gateway"],
                },
                "b": {
                    "node": "ext",
                    "interface": e_if.label,
                    "connector": jump["external_connector"],
                },
            }
        )
        console.print(
            f"  ext    jump:{je_if.label} -> {jump['external_connector']}  "
            f"{jump['external_ip']} gw {jump['external_gateway']}"
        )

    _finish(lab, link_map, args)


def _finish(lab, link_map: list, args) -> None:
    """Record the interface/address map and optionally start the lab."""
    LINK_MAP.write_text(
        "# GENERATED by scripts/build_topology.py -- do not hand-edit.\n"
        "# The authoritative record of which interface carries which address.\n"
        + yaml.safe_dump({"lab_id": lab.id, "links": link_map}, sort_keys=False)
    )
    console.print(f"\n[green]wrote[/green] {LINK_MAP.relative_to(ROOT)}")
    console.print(f"[dim]{len(lab.nodes())} nodes, {len(lab.links())} links")

    if args.start:
        console.print("starting lab...")
        lab.start(wait=True)
        console.print("[green]lab running")
    else:
        console.print("[dim]lab built but not started -- pass --start, or start in the UI")


if __name__ == "__main__":
    main()
