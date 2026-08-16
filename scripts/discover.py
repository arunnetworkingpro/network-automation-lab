#!/usr/bin/env python3
"""Inventory what this CML server can actually do, so the DC design fits the box.

Reports: compute resources, existing labs, and every available node definition
with its RAM/CPU cost. Run this before designing any topology.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from cml.client import connect

console = Console()


def main() -> None:
    client = connect()

    info = client.system_info()
    console.rule("[bold]CML system")
    console.print(f"version: {info.get('version')}   ready: {info.get('ready')}")

    # --- compute resources -------------------------------------------------
    console.rule("[bold]Compute")
    try:
        for host in client.system_management.get_compute_hosts():
            console.print(host)
    except Exception as exc:  # older/newer API shapes differ
        console.print(f"[yellow]compute host API unavailable: {exc}")

    # --- existing labs -----------------------------------------------------
    console.rule("[bold]Existing labs")
    labs = client.all_labs(show_all=True)
    if not labs:
        console.print("[dim]none")
    for lab in labs:
        console.print(
            f"{lab.id}  {lab.title!r:40} state={lab.state()}  nodes={len(lab.nodes())}"
        )

    # --- node definitions --------------------------------------------------
    console.rule("[bold]Node definitions available")
    table = Table("id", "type", "RAM MB", "CPUs", "disk GB", "description")
    defs = client.definitions.node_definitions()
    rows = []
    for nd in defs:
        did = nd.get("id", "?")
        general = nd.get("general", {}) or {}
        sim = nd.get("sim", {}) or {}
        ram = (sim.get("ram") or general.get("ram") or "")
        cpus = (sim.get("cpus") or general.get("cpus") or "")
        disk = (sim.get("disk_driver") and "" ) or ""
        nature = general.get("nature", "")
        desc = (nd.get("ui", {}) or {}).get("description", "") or ""
        rows.append((did, nature, str(ram), str(cpus), str(disk), desc.split("\n")[0][:44]))

    for row in sorted(rows, key=lambda r: (r[1], r[0])):
        table.add_row(*row)
    console.print(table)
    console.print(f"\n[dim]{len(rows)} node definitions[/dim]")


if __name__ == "__main__":
    main()
