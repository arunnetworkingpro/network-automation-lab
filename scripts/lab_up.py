#!/usr/bin/env python3
"""Bring the fabric back after a power cycle, unattended.

CML does not auto-start labs: after the controller reboots, everything comes back
STOPPED. This waits for the controller to be ready, starts the lab if it is not
already running, waits for the nodes, and re-bootstraps SSH host keys if the
switches came back without them.

Written to be safe to run at any time. If the lab is already running it does
nothing and exits 0, which is what makes it usable from @reboot cron.

    python scripts/lab_up.py            # bring it up, wait for it
    python scripts/lab_up.py --check    # report state, change nothing
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

ROOT = Path(__file__).resolve().parent.parent
LINK_MAP = ROOT / "topology" / "link-map.yml"
TOPOLOGY = ROOT / "topology" / "dc-fabric.yml"
PYTHON = ROOT / ".venv" / "bin" / "python"

# The Pi and the CML server may power on together, and the controller takes a
# while to be ready. Failing fast here would defeat the point.
CONTROLLER_WAIT = 900
NODE_WAIT = 600
# CML reports the jump BOOTED when the VM is up, which is well before sshd is
# accepting. bootstrap_ssh.py drives the switches *through* the jump, so it has
# to wait for that separately or it fails on a fabric that is merely slow.
JUMP_SSH_WAIT = 300
BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_RETRY_WAIT = 60

# BOOTED only, deliberately. CML sets STARTED the moment the node process is
# running and BOOTED when the node reports boot complete -- so accepting STARTED
# passes the gate on the first poll, ~30s in, with the switches still booting.
# Every node type in this lab, external connector included, reaches BOOTED.
READY_STATES = ("BOOTED",)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for_ssh(host: str, deadline: float) -> bool:
    """True once something is listening on 22, False if we run out of time."""
    while time.time() < deadline:
        try:
            with socket.create_connection((host, 22), timeout=5):
                return True
        except OSError:
            time.sleep(10)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report state, change nothing")
    args = ap.parse_args()

    lab_id = yaml.safe_load(LINK_MAP.read_text())["lab_id"]

    # --- wait for the controller ------------------------------------------
    from cml.client import connect

    deadline = time.time() + CONTROLLER_WAIT
    client = None
    while time.time() < deadline:
        try:
            client = connect()
            client.system_info()
            break
        except Exception as exc:
            log(f"controller not ready ({type(exc).__name__}), retrying")
            time.sleep(20)
    if client is None:
        sys.exit("controller never became ready")

    lab = client.join_existing_lab(lab_id)
    log(f"lab {lab.title!r} state={lab.state()}")

    if args.check:
        for n in sorted(lab.nodes(), key=lambda n: n.label):
            print(f"  {n.label:10} {n.state}")
        return 0

    if lab.state() != "STARTED":
        log("starting lab")
        lab.start(wait=False)
    else:
        log("lab already started")

    # --- wait for the nodes ------------------------------------------------
    deadline = time.time() + NODE_WAIT
    states: dict[str, str] = {}
    while True:
        states = {n.label: n.state for n in lab.nodes()}
        if all(s in READY_STATES for s in states.values()):
            break
        if time.time() >= deadline:
            break
        time.sleep(15)

    for label in sorted(states):
        log(f"  {label:10} {states[label]}")

    stalled = sorted(l for l, s in states.items() if s not in READY_STATES)
    if stalled:
        # Unattended, so this must be loud. A fabric that never booted looks
        # exactly like a healthy one if the only signal is "the script ran".
        log(f"FAILED: not booted after {NODE_WAIT}s: {', '.join(stalled)}")
        return 1

    # --- SSH host keys ------------------------------------------------------
    # These live in NVRAM and normally survive, but a node that gets wiped comes
    # back without one. bootstrap_ssh.py skips anything already listening, so this
    # costs nothing when there is nothing to do. It reaches the switches *through*
    # the jump, so the jump has to be answering first.
    jump_ip = yaml.safe_load(TOPOLOGY.read_text())["jump_server"]["external_ip"].split("/")[0]
    log(f"waiting for jump {jump_ip} to accept SSH")
    if not wait_for_ssh(jump_ip, time.time() + JUMP_SSH_WAIT):
        log(f"FAILED: jump {jump_ip} not accepting SSH after {JUMP_SSH_WAIT}s")
        return 1

    # Retried, because BOOTED is not the same as "IOS is answering". A leaf can be
    # BOOTED with neither SSH nor telnet up yet, and bootstrap's telnet attempt is
    # then refused. It is idempotent and its closing sweep is authoritative, so the
    # cheap correct answer is to run it again rather than trust one pass.
    for attempt in range(1, BOOTSTRAP_ATTEMPTS + 1):
        log(f"checking SSH host keys (attempt {attempt}/{BOOTSTRAP_ATTEMPTS})")
        rc = subprocess.run(
            [str(PYTHON), str(ROOT / "scripts" / "bootstrap_ssh.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        for line in rc.stdout.strip().splitlines()[-8:]:
            log(f"  {line}")
        if rc.returncode == 0:
            break
        for line in rc.stderr.strip().splitlines()[-5:]:
            log(f"  stderr: {line}")
        if attempt < BOOTSTRAP_ATTEMPTS:
            log(f"  exited {rc.returncode}, retrying in {BOOTSTRAP_RETRY_WAIT}s")
            time.sleep(BOOTSTRAP_RETRY_WAIT)
    else:
        log(f"FAILED: bootstrap_ssh.py still exiting {rc.returncode}")
        return 1

    log("fabric up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
