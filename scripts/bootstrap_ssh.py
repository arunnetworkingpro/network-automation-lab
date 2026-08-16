#!/usr/bin/env python3
"""Give every switch an SSH host key, so Ansible has something to connect to.

Why this exists: IOS will not generate an RSA key from a startup-config -- it is an
exec-mode action. On the spines (iol-xe) an EEM applet handles it at boot. On the
leaves it cannot: the ioll2-xe image has no EEM whatsoever, and silently discards
'event manager' lines rather than rejecting them.

So the leaves are bootstrapped the only way left: telnet in, generate the key, leave.
After that SSH works and telnet is never needed again -- until the next
`gen_configs.py --push`, which wipes the node and takes the key with it. Re-run this
afterwards. It is idempotent; a device that already answers on 22 is skipped.

Everything is driven from the jump box, because the Pi has no route into the fabric
until the home router learns 10.0.0.0/8.

    python scripts/bootstrap_ssh.py
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paramiko
import yaml
from rich.console import Console

from cml.client import load_env

console = Console()
ROOT = Path(__file__).resolve().parent.parent
TOPOLOGY = ROOT / "topology" / "dc-fabric.yml"

# Runs on the jump, not here. Python 3.13 removed telnetlib, so this speaks just
# enough of the protocol to refuse every option the switch offers and get a prompt.
REMOTE = r'''python3 - <<'PYEOF'
import socket, sys, time
HOST, USER, PW = "__HOST__", "__USER__", "__PW__"
IAC = 255

def negotiate(data, s):
    out, i = bytearray(), 0
    while i < len(data):
        b = data[i]
        if b == IAC and i + 2 < len(data):
            cmd, opt = data[i+1], data[i+2]
            if cmd in (253, 254):      # DO / DONT  -> WONT
                s.sendall(bytes([IAC, 252, opt]))
            elif cmd in (251, 252):    # WILL / WONT -> DONT
                s.sendall(bytes([IAC, 254, opt]))
            i += 3
        else:
            out.append(b); i += 1
    return bytes(out)

try:
    s = socket.create_connection((HOST, 23), timeout=15)
except Exception as exc:
    print("TELNET-FAIL", exc); sys.exit(1)
s.settimeout(5)

def read(t=3.0):
    end, out = time.time() + t, b""
    while time.time() < end:
        try:
            d = s.recv(4096)
            if not d: break
            out += negotiate(d, s)
        except Exception:
            break
    return out.decode(errors="ignore")

read(3)
s.sendall(USER.encode() + b"\r\n"); time.sleep(1.5); read(2)
s.sendall(PW.encode() + b"\r\n");   time.sleep(2.5); read(2)
for cmd, wait in [("terminal length 0", 2),
                  ("configure terminal", 2),
                  ("crypto key generate rsa modulus 2048", 8),
                  ("end", 2),
                  ("write memory", 5),
                  ("show ip ssh | include SSH", 3)]:
    s.sendall(cmd.encode() + b"\r\n"); time.sleep(wait)
    out = read(wait)
    if "SSH Enabled" in out:
        print("SSH-OK")
    if "Invalid input" in out or "% Error" in out:
        print("CMD-ERROR", cmd, out.strip()[-120:])
PYEOF'''


def main() -> None:
    topo = yaml.safe_load(TOPOLOGY.read_text())
    env = load_env()
    pw = env.get("LAB_DEVICE_PASS")
    if not pw:
        sys.exit("LAB_DEVICE_PASS missing from ~/.cml.env -- run gen_configs.py first")

    jump_ip = topo["jump_server"]["external_ip"].split("/")[0]
    devices = [(d["name"], d["loopback"]) for d in topo["spines"] + topo["leaves"]]

    jump = paramiko.SSHClient()
    jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    console.print(f"via jump {jump_ip}")
    jump.connect(jump_ip, username="arun", password=pw, timeout=30)

    for name, ip in devices:
        # Already listening? Then it has a key; nothing to do.
        probe = f"python3 -c \"import socket;s=socket.socket();s.settimeout(3);s.connect(('{ip}',22))\""
        _, out, _ = jump.exec_command(probe)
        if out.channel.recv_exit_status() == 0:
            console.print(f"  [dim]{name:8} {ip:10} SSH already up, skipping")
            continue

        console.print(f"  {name:8} {ip:10} no SSH -- bootstrapping over telnet")
        # Plain substitution, not %-formatting: the remote script contains a literal
        # "% Error" that %-formatting would try to interpret.
        script = (
            REMOTE.replace("__HOST__", ip).replace("__USER__", "arun").replace("__PW__", pw)
        )
        _, out, err = jump.exec_command(script, timeout=120)
        text = out.read().decode(errors="ignore") + err.read().decode(errors="ignore")

        if "SSH-OK" in text:
            console.print(f"           [green]key generated, SSH up[/green]")
        elif "TELNET-FAIL" in text:
            console.print(f"           [red]telnet refused -- is the node booted?[/red]")
        else:
            console.print(f"           [yellow]unclear: {text.strip()[-140:]}")

    # Final sweep, so the exit status means something.
    console.rule("[bold]result")
    failed = 0
    for name, ip in devices:
        probe = f"python3 -c \"import socket;s=socket.socket();s.settimeout(3);s.connect(('{ip}',22))\""
        _, out, _ = jump.exec_command(probe)
        ok = out.channel.recv_exit_status() == 0
        console.print(f"  {'[green]SSH[/green]' if ok else '[red]---[/red]'}  {name:8} {ip}")
        failed += 0 if ok else 1

    jump.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
