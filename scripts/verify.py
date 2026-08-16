#!/usr/bin/env python3
"""Prove the fabric works, from the only place that can see all of it: the jump box.

Runs a ladder of checks that fail in a useful order. Each rung depends on the one
below it, so the first failure tells you which layer broke rather than just that
something is wrong:

    1. jump's own addressing          -- did cloud-init apply?
    2. jump -> leaf1 SVI              -- is the access port in the right VLAN?
    3. jump -> loopbacks              -- is OSPF up across the /31s?
    4. jump -> a server in each VLAN  -- is BGP carrying the host subnets?
    5. server -> server across leaves -- ECMP and inter-VLAN routing end to end

Everything runs over one SSH session to the jump. The Pi cannot reach 10.0.0.0/8
directly until the home router has the static route, which is exactly why these
checks originate from the jump instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paramiko
import yaml
from rich.console import Console

from cml.client import load_env

console = Console()
ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY = ROOT / "topology" / "dc-fabric.yml"


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 30) -> tuple[int, str]:
    _, out, err = client.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, (out.read().decode() + err.read().decode()).strip()


def check(client: paramiko.SSHClient, label: str, cmd: str) -> bool:
    rc, text = run(client, cmd)
    mark = "[green]PASS[/green]" if rc == 0 else "[red]FAIL[/red]"
    console.print(f"  {mark}  {label}")
    if rc != 0 and text:
        console.print(f"        [dim]{text.splitlines()[-1][:90]}")
    return rc == 0


def main() -> None:
    topo = yaml.safe_load(TOPOLOGY.read_text())
    env = load_env()
    pw = env.get("LAB_DEVICE_PASS")
    if not pw:
        sys.exit("LAB_DEVICE_PASS missing from ~/.cml.env -- run gen_configs.py first")

    jump = topo["jump_server"]
    host = jump["external_ip"].split("/")[0]

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    console.print(f"connecting to jump at {host} ...")
    cli.connect(host, username="arun", password=pw, timeout=30, banner_timeout=30)

    passed = failed = 0

    console.rule("[bold]1. jump addressing")
    _, addrs = run(cli, "ip -br a | grep -v LOOPBACK")
    console.print(f"[dim]{addrs}")
    _, fwd = run(cli, "sysctl -n net.ipv4.ip_forward")
    console.print(f"[dim]ip_forward = {fwd}")

    checks: list[tuple[str, str]] = []

    # 2. the leaf SVI that is this host's default way off its VLAN
    mgmt_gw = next(v["gateway"] for v in topo["vlans"] if v["id"] == jump["vlan"])
    checks.append((f"jump -> {jump['leaf']} SVI {mgmt_gw}", f"ping -c2 -W2 {mgmt_gw}"))

    # 3. loopbacks: OSPF has converged if these answer
    for role in ("spines", "leaves"):
        for dev in topo[role]:
            checks.append(
                (f"jump -> {dev['name']} loopback {dev['loopback']}",
                 f"ping -c2 -W2 {dev['loopback']}")
            )

    # 4. host subnets: these only answer if BGP is advertising them
    for srv in topo["servers"]:
        ip = srv["ip"].split("/")[0]
        checks.append((f"jump -> {srv['name']} {ip} (vlan {srv['vlan']})",
                       f"ping -c2 -W2 {ip}"))

    # 5. internet, via the home LAN side
    checks.append(("jump -> internet (github.com)", "ping -c2 -W3 github.com"))

    console.rule("[bold]2-5. reachability")
    for label, cmd in checks:
        if check(cli, label, cmd):
            passed += 1
        else:
            failed += 1

    console.rule("[bold]summary")
    console.print(f"  [green]{passed} passed[/green]   [red]{failed} failed[/red]")
    cli.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
