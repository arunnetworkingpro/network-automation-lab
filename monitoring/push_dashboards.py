#!/usr/bin/env python3
"""Push monitoring/dashboards/*.json into Grafana Cloud over the HTTP API.

    python3 monitoring/push_dashboards.py

Beats copy-pasting JSON into the import box: it is idempotent (same uid ->
update, not a second copy), it survives a laptop that cannot reach the Pi, and
regenerating plus re-pushing after a topology change is two commands.

Credentials come from files, never the command line, so nothing lands in shell
history:

    ~/.grafana-api-url     https://<stack>.grafana.net
    ~/.grafana-api-token   a service-account token with the Editor role

The generated JSON leaves the dashboard's datasource variable empty on purpose
-- the uid is per-stack and does not belong in a committed file. This script
resolves the default Prometheus datasource at push time and stamps it in, so the
dashboard renders on first open instead of greeting you with an empty picker.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARDS = HERE / "dashboards"
FOLDER_TITLE = "CML Lab"


def _read_secret(path: Path) -> str:
    if not path.exists():
        sys.exit(f"missing {path} -- see the docstring in {Path(__file__).name}")
    value = path.read_text().strip()
    if not value:
        sys.exit(f"{path} is empty")
    return value


def _api(base: str, token: str, path: str, payload=None, method=None):
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method or ("POST" if payload is not None else "GET"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method or 'GET'} {path} -> {e.code}: {e.read().decode()[:400]}")
    except urllib.error.URLError as e:
        # Wrong ~/.grafana-api-url, DNS down, no uplink -- a readable line beats
        # a traceback, since this is the failure a fresh setup actually hits.
        sys.exit(f"cannot reach {base}: {e.reason}")


def default_prom_source(base: str, token: str) -> tuple[str, str]:
    """Return (uid, name). Grafana's datasource variable wants both."""
    sources = _api(base, token, "/api/datasources")
    prom = [d for d in sources if d["type"] == "prometheus"]
    if not prom:
        sys.exit("no prometheus datasource on this stack")
    # The stack's own hosted metrics, not the grafanacloud-usage billing source.
    chosen = next((d for d in prom if d.get("isDefault")), prom[0])
    return chosen["uid"], chosen["name"]


def ensure_folder(base: str, token: str) -> str | None:
    for f in _api(base, token, "/api/folders") or []:
        if f["title"] == FOLDER_TITLE:
            return f["uid"]
    created = _api(base, token, "/api/folders", {"title": FOLDER_TITLE})
    return created["uid"]


def stamp_datasource(dash: dict, uid: str, name: str) -> dict:
    """Point the dashboard's datasource variable at a real source.

    text is the display *name* and value the uid. Putting the uid in both makes
    the picker show a raw uid, and since the variable refreshes on load Grafana
    cannot match that text to any datasource and falls back to unset.
    """
    for var in dash.get("templating", {}).get("list", []):
        if var.get("type") == "datasource":
            var["current"] = {"text": name, "value": uid, "selected": True}
    return dash


def main() -> None:
    base = _read_secret(Path.home() / ".grafana-api-url").rstrip("/")
    token = _read_secret(Path.home() / ".grafana-api-token")

    uid, name = default_prom_source(base, token)
    folder = ensure_folder(base, token)
    print(f"stack {base}  datasource {name} ({uid})  folder {FOLDER_TITLE}")

    files = sorted(DASHBOARDS.glob("*.json"))
    if not files:
        sys.exit(f"no dashboards in {DASHBOARDS} -- run gen_dashboards.py first")

    for path in files:
        dash = stamp_datasource(json.loads(path.read_text()), uid, name)
        # Let Grafana own the version counter; sending a stale one is a 412.
        dash.pop("version", None)
        dash.pop("id", None)
        res = _api(base, token, "/api/dashboards/db", {
            "dashboard": dash,
            "folderUid": folder,
            "overwrite": True,
            "message": f"pushed from {path.name}",
        })
        print(f"  {path.name:22} -> {base}{res['url']}")


if __name__ == "__main__":
    main()
