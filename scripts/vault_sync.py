"""Write every lab device login into 1Password, idempotently.

Reads the device password from ~/.cml.env and the device list from the topology,
then creates or updates one Login item per device group in the vault named below.
Secrets go to `op` on stdin only -- never argv, never stdout.

The service account token lives in ~/.op-token (chmod 600). That account is
read-only on the 'arunlab' vault, so items go in a vault it created itself,
where it has full access.

Usage:  .venv/bin/python scripts/vault_sync.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

VAULT = "arunlab-devices"
OP = str(Path.home() / ".local/bin/op")
REPO = Path(__file__).resolve().parent.parent


def op(*args, stdin=None):
    env = dict(os.environ, OP_SERVICE_ACCOUNT_TOKEN=(Path.home() / ".op-token").read_text().strip())
    r = subprocess.run([OP, *args, "--format", "json"], input=stdin,
                       capture_output=True, text=True, env=env)
    if r.returncode:
        sys.exit(f"op {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def load_env():
    env = {}
    for line in (Path.home() / ".cml.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def items(topo, pw, cml_host):
    spines = ", ".join(f"{s['name']} {s['loopback']}" for s in topo["spines"])
    leaves = ", ".join(f"{l['name']} {l['loopback']}" for l in topo["leaves"])
    jump = topo["jump_server"]
    console = f"Console: ssh arun@{cml_host} (CML UI creds), then open /<lab>/<node>/0."
    out = [
        ("CML switches (spines + leaves)", [("enable secret", pw)],
         f"IOL, lab {topo['lab']['title']}. Spines: {spines}. Leaves: {leaves}. "
         f"SSH from the jump host. {console}"),
        ("CML jump host", [],
         f"Ubuntu. LAN {jump['external_ip'].split('/')[0]}, entry point into the fabric "
         f"and NAT gateway for 10.10.0.0/16. {console}"),
    ]
    for h in topo.get("rdma_hosts", []):
        out.append((f"CML {h['name']} (RDMA host)", [],
                    f"Ubuntu + Soft-RoCE. Fabric {h['fabric_ip'].split('/')[0]} "
                    f"({h['leaf']} VLAN {h['vlan']}). Reach via the jump host. {console}"))
    out.append(("CML Alpine servers", [],
                f"srv-app1 / srv-db1, addresses by DHCP from the leaves. {console}"))
    return out


def main():
    env = load_env()
    pw = env["LAB_DEVICE_PASS"]
    topo = yaml.safe_load((REPO / "topology/dc-fabric.yml").read_text())

    if not any(v["name"] == VAULT for v in op("vault", "list")):
        op("vault", "create", VAULT, "--description", "CML lab device logins (scripts/vault_sync.py)")
        print(f"created vault {VAULT}")

    existing = {i["title"]: i["id"] for i in op("item", "list", "--vault", VAULT) or []}
    cml_host = env["CML_HOST"].removeprefix("https://").rstrip("/")
    for title, extra, notes in items(topo, pw, cml_host):
        fields = [
            {"id": "username", "type": "STRING", "purpose": "USERNAME", "label": "username", "value": "arun"},
            {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "label": "password", "value": pw},
            {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain", "value": notes},
        ] + [{"type": "CONCEALED", "label": k, "value": v} for k, v in extra]
        tpl = json.dumps({"title": title, "category": "LOGIN", "tags": ["cml-lab"], "fields": fields})
        if title in existing:
            op("item", "edit", existing[title], "--vault", VAULT, stdin=tpl)
            print(f"updated  {title}")
        else:
            op("item", "create", "--vault", VAULT, stdin=tpl)
            print(f"created  {title}")


if __name__ == "__main__":
    main()
