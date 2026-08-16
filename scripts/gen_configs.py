#!/usr/bin/env python3
"""Render day-0 configs for every node, from the topology and the real link map.

Nothing here is typed twice. Interface names come from link-map.yml -- what the
controller actually handed back -- rather than from what we hoped the numbering
would be. Addresses come from dc-fabric.yml. Change either file and re-run.

    python scripts/gen_configs.py           # render to configs/
    python scripts/gen_configs.py --push    # render, then apply and reboot nodes

--push is not additive: a day-0 config only takes effect at boot, so pushing stops
the affected nodes, sets the config, and starts them again.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from netaddr import IPNetwork
from rich.console import Console

from cml.client import connect, load_env

console = Console()

ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY = ROOT / "topology" / "dc-fabric.yml"
LINK_MAP = ROOT / "topology" / "link-map.yml"
CONFIGS = ROOT / "configs"
TEMPLATES = ROOT / "templates"

DOMAIN = "lab.local"
DNS = ["192.168.2.1", "1.1.1.1"]


def device_password() -> str:
    """Lab device credential, from ~/.cml.env -- generated once if absent.

    Deliberately not a default like 'cisco' and deliberately not in the repo. If
    the key is missing we mint a random one and write it next to the CML password,
    so the secret exists in exactly one place that is already chmod 600.
    """
    env = load_env()
    if env.get("LAB_DEVICE_PASS"):
        return env["LAB_DEVICE_PASS"]

    pw = secrets.token_urlsafe(12)
    env_file = Path.home() / ".cml.env"
    with env_file.open("a") as fh:
        fh.write(f"LAB_DEVICE_PASS={pw}\n")
    env_file.chmod(0o600)
    console.print(
        "[yellow]LAB_DEVICE_PASS was not set -- generated one and appended it to "
        "~/.cml.env[/yellow]\n[dim]Read it with: grep LAB_DEVICE_PASS ~/.cml.env"
    )
    return pw


def netmask(cidr: str) -> str:
    return str(IPNetwork(cidr).netmask)


def build_context(topo: dict, links: list) -> dict:
    """Turn the flat link list into per-node views of the fabric."""
    by_node: dict[str, list] = {}
    for link in links:
        for near, far in ((link["a"], link["b"]), (link["b"], link["a"])):
            by_node.setdefault(near["node"], []).append((near, far))
    return by_node


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="apply configs and reboot nodes")
    ap.add_argument(
        "--only",
        default="",
        help="comma-separated node labels to push (default: all). Pushing reboots a "
        "node, so scope it when you only changed one thing.",
    )
    args = ap.parse_args()
    only = {n.strip() for n in args.only.split(",") if n.strip()}

    topo = yaml.safe_load(TOPOLOGY.read_text())
    lm = yaml.safe_load(LINK_MAP.read_text())
    links = lm["links"]
    by_node = build_context(topo, links)

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,  # a missing variable should fail loudly, not silently
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )

    asn = topo["underlay"]["bgp_asn"]
    area = topo["underlay"]["ospf_area"]
    vlans = {v["id"]: v for v in topo["vlans"]}

    # Phase 2 overlay. Absent or disabled and every template falls back to the
    # Phase 1 config exactly as it was -- the EVPN blocks are guarded, not merged in.
    overlay = topo.get("overlay") or {}
    if not overlay.get("enabled"):
        overlay = {}
    stretched = sorted(overlay.get("stretched", [])) if overlay else []
    for v in stretched:
        if v not in vlans:
            sys.exit(f"overlay.stretched lists VLAN {v}, which is not in vlans:")
    spine_names = [s["name"] for s in topo["spines"]]
    leaf_names = [l["name"] for l in topo["leaves"]]
    spine_lo = [s["loopback"] for s in topo["spines"]]
    leaf_lo = [l["loopback"] for l in topo["leaves"]]
    user = "arun"
    password = device_password()

    CONFIGS.mkdir(exist_ok=True)
    rendered: dict[str, str] = {}

    # --- spines ------------------------------------------------------------
    for spine in topo["spines"]:
        name = spine["name"]
        uplinks = [
            {"intf": near["interface"], "ip": near["ip"].split("/")[0], "peer": far["node"]}
            for near, far in by_node[name]
        ]
        rendered[name] = env.get_template("spine.j2").render(
            name=name,
            domain=DOMAIN,
            user=user,
            password=password,
            loopback=spine["loopback"],
            area=area,
            asn=asn,
            links=sorted(uplinks, key=lambda x: x["intf"]),
            clients=leaf_lo,
            overlay=overlay,
        )

    # --- leaves ------------------------------------------------------------
    jump = topo.get("jump_server", {})
    for leaf in topo["leaves"]:
        name = leaf["name"]
        uplinks, access = [], []
        local_vlans: set[int] = set()

        for near, far in by_node[name]:
            if far["node"] in spine_names:
                uplinks.append(
                    {
                        "intf": near["interface"],
                        "ip": near["ip"].split("/")[0],
                        "peer": far["node"],
                    }
                )
            else:
                vlan = near.get("access_vlan")
                access.append(
                    {"intf": near["interface"], "host": far["node"], "vlan": vlan}
                )
                local_vlans.add(vlan)

        # A stretched VLAN exists on every leaf, whether or not a host sits on this
        # one -- an overlay whose VLANs only appear where a host already is would be
        # pointless. It gets the VLAN, the EVPN instance and the VNI; it does not get
        # an SVI, because the anycast gateway is Phase 2b.
        l2_vlans = sorted(local_vlans | set(stretched))
        evpn_vlans = [
            {
                "id": v,
                "name": vlans[v]["name"],
                "evi": v,
                "vni": overlay["vni_base"] + v,
            }
            for v in stretched
        ]

        # An SVI only where a host in that VLAN actually lives -- see leaf.j2.
        svis = [
            {
                "id": v,
                "name": vlans[v]["name"],
                "gateway": vlans[v]["gateway"],
                "netmask": netmask(vlans[v]["subnet"]),
            }
            for v in sorted(local_vlans)
        ]
        networks = [
            {
                "network": str(IPNetwork(vlans[v]["subnet"]).network),
                "netmask": netmask(vlans[v]["subnet"]),
            }
            for v in sorted(local_vlans)
        ]

        # The leaf hosting the jump owns the way back to the home LAN, and
        # advertises it so the rest of the fabric learns it via BGP.
        static_routes = []
        if jump.get("enabled") and jump.get("leaf") == name and jump.get("external"):
            home = IPNetwork(jump["external_ip"])
            static_routes.append(
                {
                    "network": str(home.network),
                    "netmask": str(home.netmask),
                    "via": jump["fabric_ip"].split("/")[0],
                }
            )
            networks.append(
                {"network": str(home.network), "netmask": str(home.netmask)}
            )

        # One DHCP pool per host-bearing VLAN, narrowed to a single address.
        # Excluding everything on either side of the host's IP means the client
        # can only be offered that one address -- deterministic, with no MAC
        # reservation to keep in sync.
        dhcp_pools = []
        for srv in topo["servers"]:
            if srv["leaf"] != name:
                continue
            net = IPNetwork(vlans[srv["vlan"]]["subnet"])
            host = IPNetwork(srv["ip"]).ip
            dhcp_pools.append(
                {
                    "name": srv["name"],
                    "network": str(net.network),
                    "netmask": str(net.netmask),
                    "gateway": vlans[srv["vlan"]]["gateway"],
                    "dns": DNS[0],
                    "exclude_low_start": str(net.network + 1),
                    "exclude_low_end": str(host - 1),
                    "exclude_high_start": str(host + 1),
                    "exclude_high_end": str(net.broadcast - 1),
                }
            )

        rendered[name] = env.get_template("leaf.j2").render(
            dhcp_pools=dhcp_pools,
            name=name,
            domain=DOMAIN,
            user=user,
            password=password,
            loopback=leaf["loopback"],
            area=area,
            asn=asn,
            vlans=[vlans[v] for v in l2_vlans],
            uplinks=sorted(uplinks, key=lambda x: x["intf"]),
            access_ports=sorted(access, key=lambda x: x["intf"]),
            svis=svis,
            networks=networks,
            spines=spine_lo,
            static_routes=static_routes,
            overlay=overlay,
            evpn_vlans=evpn_vlans,
        )

    # --- servers -----------------------------------------------------------
    for srv in topo["servers"]:
        name = srv["name"]
        near = next(n for n, f in by_node[name])
        vlan = vlans[srv["vlan"]]
        rendered[name] = env.get_template("server.j2").render(
            name=name,
            user=user,
            password=password,
            iface=near["interface"],
            ip=srv["ip"].split("/")[0],
            prefix=IPNetwork(srv["ip"]).prefixlen,
            netmask=netmask(srv["ip"]),
            gateway=vlan["gateway"],
            vlan=srv["vlan"],
            leaf=srv["leaf"],
        )

    # --- jump --------------------------------------------------------------
    if jump.get("enabled"):
        fab = next(
            n for n, f in by_node[jump["name"]] if f["node"] in leaf_names
        )
        ext = next(
            n for n, f in by_node[jump["name"]] if f["node"] not in leaf_names
        )
        rendered[jump["name"]] = env.get_template("jump.j2").render(
            name=jump["name"],
            user=user,
            password=password,
            ssh_key=None,
            fab_if=fab["interface"],
            fab_ip=jump["fabric_ip"],
            fab_vlan=jump["vlan"],
            fab_peer=jump["leaf"],
            fab_gateway=vlans[jump["vlan"]]["gateway"],
            ext_if=ext["interface"],
            ext_ip=jump["external_ip"],
            ext_gateway=jump["external_gateway"],
            dns=DNS,
        )

    for name, text in rendered.items():
        suffix = "yaml" if text.startswith("#cloud-config") else "cfg"
        path = CONFIGS / f"{name}.{suffix}"
        path.write_text(text)
        console.print(f"  rendered [cyan]{path.relative_to(ROOT)}[/cyan] ({len(text)} bytes)")

    if not args.push:
        console.print("\n[dim]not pushed -- review configs/, then re-run with --push")
        return

    # --- push --------------------------------------------------------------
    client = connect()
    lab = client.join_existing_lab(lm["lab_id"])
    console.print(f"\npushing to [bold]{lab.title}[/bold]")

    for node in lab.nodes():
        if node.label not in rendered:
            continue
        if only and node.label not in only:
            continue
        console.print(f"  {node.label}: stopping")
        node.stop(wait=True)
        node.wipe(wait=True)  # clear any state from the previous boot
        node.configuration = rendered[node.label]
        node.start(wait=False)
        console.print(f"  {node.label}: config applied, starting")

    console.print("\n[green]pushed. Nodes are rebooting with their day-0 config.")


if __name__ == "__main__":
    main()
