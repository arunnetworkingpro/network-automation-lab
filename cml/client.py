"""Authenticated CML client.

Credentials come from ~/.cml.env (chmod 600, never committed). See example.env.
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

ENV_CANDIDATES = [
    Path.home() / ".cml.env",
    Path(__file__).resolve().parent.parent / ".cml.env",
]


def load_env() -> dict[str, str]:
    """Read the first .cml.env we find into a dict. Real env vars win."""
    values: dict[str, str] = {}
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("'\"")
        break
    else:
        sys.exit(
            "No .cml.env found. Create ~/.cml.env from example.env and chmod 600 it."
        )

    for key in ("CML_HOST", "CML_USER", "CML_PASS"):
        if key in os.environ:
            values[key] = os.environ[key]
        if not values.get(key):
            sys.exit(f"{key} missing from .cml.env")
    return values


def connect(verify_ssl: bool | None = None):
    """Return a logged-in ClientLibrary."""
    from virl2_client import ClientLibrary

    env = load_env()
    if verify_ssl is None:
        verify_ssl = env.get("CML_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

    if not verify_ssl:
        ssl._create_default_https_context = ssl._create_unverified_context

    url = env["CML_HOST"]
    if not url.startswith("http"):
        url = f"https://{url}"

    client = ClientLibrary(
        url,
        env["CML_USER"],
        env["CML_PASS"],
        ssl_verify=verify_ssl,
        allow_http=False,
        # Client 2.10 vs server 2.9 differ only cosmetically for what we use;
        # don't let the version guard abort the run.
        raise_for_auth_failure=True,
    )
    return client


if __name__ == "__main__":
    c = connect()
    print(f"Connected to CML {c.system_info().get('version')} as {load_env()['CML_USER']}")
