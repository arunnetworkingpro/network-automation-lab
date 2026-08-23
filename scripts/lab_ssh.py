#!/usr/bin/env python3
"""Run commands on a Linux lab host over SSH.

    from scripts.lab_ssh import Host
    with Host("192.168.2.50") as h:
        h.run("uname -r")
        h.sudo("apt-get install -y foo")

Password auth on purpose: these nodes are rebuilt from `gen_configs.py --push`
often enough that pushing keys is churn, and the credential is already in
~/.cml.env as LAB_DEVICE_PASS. Nothing here is reachable from outside the LAN.
"""

from __future__ import annotations

import select
import sys
import time
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cml.client import load_env  # noqa: E402

DEFAULT_USER = "arun"


class CommandFailed(RuntimeError):
    pass


class Host:
    def __init__(self, address: str, user: str = DEFAULT_USER, timeout: int = 30,
                 via: str | None = None):
        """via: address of a jump host to tunnel through.

        Fabric-only nodes have no route to the home LAN, so the Pi cannot reach
        them directly -- the jump box is the only way in, same as Ansible uses.
        """
        self.address, self.user, self.timeout = address, user, timeout
        self.via = via
        self._via_host: "Host | None" = None
        pw = load_env().get("LAB_DEVICE_PASS")
        if not pw:
            sys.exit("LAB_DEVICE_PASS missing from ~/.cml.env -- run gen_configs.py first")
        self._pw = pw
        self._client: paramiko.SSHClient | None = None

    @property
    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise CommandFailed(f"[{self.address}] not connected -- use `with Host(...)`")
        return self._client

    def __enter__(self) -> "Host":
        sock = None
        if self.via:
            self._via_host = Host(self.via, self.user, self.timeout).__enter__()
            sock = self._via_host.client.get_transport().open_channel(
                "direct-tcpip", (self.address, 22), ("127.0.0.1", 0))
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(self.address, username=self.user, password=self._pw,
                             timeout=self.timeout, sock=sock)
        return self

    def __exit__(self, *exc) -> None:
        if self._client:
            self._client.close()
        if self._via_host:
            self._via_host.__exit__()

    def run(self, command: str, timeout: int = 120, check: bool = True) -> str:
        """Run a command, return combined output. Raises on non-zero if check."""
        _, out, err = self.client.exec_command(command, timeout=timeout)
        text = out.read().decode() + err.read().decode()
        rc = out.channel.recv_exit_status()
        if check and rc != 0:
            raise CommandFailed(f"[{self.address}] rc={rc}: {command}\n{text}")
        return text.strip()

    def sudo(self, command: str, timeout: int = 600, check: bool = True) -> str:
        """Same, with the password fed to sudo on stdin rather than the CLI.

        select() rather than polling recv_ready(): a poll loop never blocks, so
        a ten-minute apt run would spin a full core on a 4-core Pi that is also
        carrying the exporter and Alloy. select also makes `timeout` real --
        a channel timeout only bounds blocking reads, which a poll loop never
        makes, so the deadline has to be checked by hand.
        """
        chan = self.client.get_transport().open_session()
        chan.exec_command(f"sudo -S -p '' bash -c {_quote(command)}")
        chan.sendall(self._pw + "\n")
        # EOF on stdin, or any command that reads it blocks the remote forever.
        chan.shutdown_write()

        deadline = time.monotonic() + timeout
        chunks: list[str] = []

        def drain() -> bool:
            got = False
            while chan.recv_ready():
                chunks.append(chan.recv(65536).decode(errors="replace"))
                got = True
            while chan.recv_stderr_ready():
                chunks.append(chan.recv_stderr(65536).decode(errors="replace"))
                got = True
            return got

        while True:
            select.select([chan], [], [], 1.0)
            drained = drain()
            # Only stop once the pipes are dry as well as the status ready --
            # otherwise the tail of stderr is lost, which is exactly the text
            # that explains a non-zero exit.
            if chan.exit_status_ready() and not drained and not chan.recv_ready() \
                    and not chan.recv_stderr_ready():
                break
            if time.monotonic() > deadline:
                chan.close()
                raise CommandFailed(
                    f"[{self.address}] sudo timed out after {timeout}s: {command}\n"
                    + "".join(chunks))

        text = "".join(chunks)
        rc = chan.recv_exit_status()
        chan.close()
        if check and rc != 0:
            raise CommandFailed(f"[{self.address}] sudo rc={rc}: {command}\n{text}")
        return text.strip()


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: lab_ssh.py <address> <command...>")
    with Host(sys.argv[1]) as h:
        print(h.run(" ".join(sys.argv[2:]), check=False))
