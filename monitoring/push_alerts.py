#!/usr/bin/env python3
"""Provision the CML lab's alert rules into Grafana Cloud.

    python3 monitoring/push_alerts.py

Rules as code, same reasoning as the dashboards: the whole group is replaced in
one PUT, so re-running is idempotent and the file is the source of truth. Reads
~/.grafana-api-url and ~/.grafana-api-token like push_dashboards.py.

Two design choices worth keeping:

* Node-down and link-down are multiplied by a lab-state gate, so deliberately
  stopping the lab does not page. The gate returns no series when the lab is not
  STARTED, and those rules set noDataState=OK so silence reads as "not running"
  rather than "broken".
* Everything has a `for` duration of at least 10 minutes against a 60s scrape.
  A single missed scrape on a Pi that is also running the exporter is normal;
  paging on it trains you to ignore the alerts.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

FOLDER_TITLE = "CML Lab"
GROUP = "cml-lab"
INTERVAL_SECONDS = 60  # matches the scrape; evaluating faster just re-reads

# Personal address on purpose. Nothing about this lab routes through work.
CONTACT_NAME = "arun-email"
CONTACT_EMAIL = "arun@arunnetworkingpro.com"

LAB_STARTED = '* on(lab) group_left() (cml_lab_state_info{state="STARTED"} == 1)'

MEM_USED_PCT = ('100 * cml_compute_memory_bytes{state="used"}'
                ' / ignoring(state) cml_compute_memory_bytes{state="total"}')
DISK_USED_PCT = ('100 * cml_compute_disk_bytes{state="used"}'
                 ' / ignoring(state) cml_compute_disk_bytes{state="total"}')

# uid, title, expr, comparison, threshold, for, severity, summary, no-data state
RULES = [
    ("cml-metrics-stalled", "Metrics stopped arriving",
     "up", "lt", 1, "10m", "critical",
     "A scrape target has stopped answering. If this is the only firing alert, "
     "suspect the Pi or Alloy rather than the lab.",
     "Alerting"),

    ("cml-api-unreachable", "CML API unreachable",
     "cml_up", "lt", 1, "10m", "critical",
     "The exporter cannot reach the CML REST API. Every cml_* panel is blind "
     "until this clears.",
     "Alerting"),

    ("cml-node-down", "Node not booted",
     f"cml_node_booted {LAB_STARTED}", "lt", 1, "15m", "warning",
     "A node in a STARTED lab is not BOOTED. Gated on lab state, so this does "
     "not fire when the lab is deliberately stopped.",
     "OK"),

    ("cml-link-down", "Link down",
     f"cml_link_up {LAB_STARTED}", "lt", 1, "15m", "warning",
     "A virtual wire is not STARTED while its lab is running. Expect an "
     "adjacency to be missing.",
     "OK"),

    ("cml-link-drops", "Link dropping packets",
     "rate(cml_link_drops_total[10m])", "gt", 0, "15m", "warning",
     "A link is losing frames. Sustained drops on an idle lab point at a "
     "starved vSwitch on the compute, not at the topology.",
     "OK"),

    ("cml-oversubscribed", "CPU heavily oversubscribed",
     "cml_compute_cpu_predicted / cml_compute_cpu_count", "gt", 3, "20m", "warning",
     "CML sizes the running nodes at more than 3x the six physical cores. "
     "Boots will start timing out before anything else breaks.",
     "OK"),

    ("cml-compute-memory", "Compute memory nearly full",
     MEM_USED_PCT, "gt", 90, "15m", "warning",
     "Memory has historically not been the constraint here, so this firing "
     "means something changed.",
     "OK"),

    ("cml-compute-disk", "Compute disk nearly full",
     DISK_USED_PCT, "gt", 90, "30m", "critical",
     "CML cannot start nodes or take snapshots on a full disk, and it fails in "
     "confusing ways rather than saying so.",
     "OK"),

    ("cml-pi-temp", "Pi running hot",
     'max(node_thermal_zone_temp{job="pi"})', "gt", 75, "15m", "warning",
     "Thermal throttling starts around 80 C. The monitoring itself gets "
     "unreliable before the Pi actually stops.",
     "OK"),

    ("cml-pi-disk", "Pi disk nearly full",
     '100 - 100 * node_filesystem_avail_bytes{job="pi",mountpoint="/"}'
     ' / node_filesystem_size_bytes{job="pi",mountpoint="/"}',
     "gt", 90, "30m", "warning",
     "Alloy buffers metrics to disk on the Pi. A full root filesystem loses "
     "data silently.",
     "OK"),
]


def _read_secret(path: Path) -> str:
    if not path.exists():
        sys.exit(f"missing {path} -- see push_dashboards.py for how these are made")
    value = path.read_text().strip()
    if not value:
        sys.exit(f"{path} is empty")
    return value


def _api(base, token, path, payload=None, method=None):
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method or ("POST" if payload is not None else "GET"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Without this the rules import as file-provisioned and the UI
            # refuses to let you edit or silence them by hand.
            "X-Disable-Provenance": "true",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method or 'GET'} {path} -> {e.code}: {e.read().decode()[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {base}: {e.reason}")


def build_rule(uid, title, expr, op, threshold, for_, severity, summary,
               nodata, folder_uid, ds_uid):
    prom = {
        "refId": "A",
        "relativeTimeRange": {"from": 600, "to": 0},
        "datasourceUid": ds_uid,
        "model": {
            "refId": "A",
            "expr": expr,
            "instant": True,
            "range": False,
            "editorMode": "code",
            "intervalMs": 60000,
            "maxDataPoints": 43200,
            "datasource": {"type": "prometheus", "uid": ds_uid},
        },
    }
    condition = {
        "refId": "B",
        "relativeTimeRange": {"from": 600, "to": 0},
        "datasourceUid": "__expr__",
        "model": {
            "refId": "B",
            "type": "threshold",
            "expression": "A",
            "datasource": {"type": "__expr__", "uid": "__expr__"},
            "conditions": [{
                "type": "query",
                "evaluator": {"type": op, "params": [threshold]},
                "operator": {"type": "and"},
                "query": {"params": ["B"]},
                "reducer": {"type": "last", "params": []},
            }],
        },
    }
    return {
        "uid": uid,
        "title": title,
        "folderUID": folder_uid,
        "ruleGroup": GROUP,
        "condition": "B",
        "for": for_,
        "orgID": 1,
        "noDataState": nodata,
        "execErrState": "Error",
        "labels": {"severity": severity, "source": "cml-lab"},
        "annotations": {"summary": summary, "__expr__": expr},
        "data": [prom, condition],
    }


def ensure_contact_point(base, token) -> None:
    """Create or update the email destination, addressed by name."""
    existing = _api(base, token, "/api/v1/provisioning/contact-points") or []
    body = {
        "name": CONTACT_NAME,
        "type": "email",
        "settings": {"addresses": CONTACT_EMAIL, "singleEmail": False},
        "disableResolveMessage": False,  # send the all-clear too, not just the bad news
    }
    match = next((c for c in existing if c["name"] == CONTACT_NAME), None)
    if match:
        _api(base, token,
             f"/api/v1/provisioning/contact-points/{match['uid']}",
             body | {"uid": match["uid"]}, method="PUT")
    else:
        _api(base, token, "/api/v1/provisioning/contact-points", body)


def set_policy_tree(base, token) -> None:
    """Route everything to email, with critical on a shorter leash.

    Grouping by alertname matters here: 'Link down' across eight links is one
    email listing eight links, not eight emails.
    """
    _api(base, token, "/api/v1/provisioning/policies", {
        "receiver": CONTACT_NAME,
        "group_by": ["grafana_folder", "alertname"],
        "group_wait": "1m",
        "group_interval": "5m",
        "repeat_interval": "12h",
        "routes": [{
            "receiver": CONTACT_NAME,
            "object_matchers": [["severity", "=", "critical"]],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        }],
    }, method="PUT")


def main() -> None:
    base = _read_secret(Path.home() / ".grafana-api-url").rstrip("/")
    token = _read_secret(Path.home() / ".grafana-api-token")

    folders = _api(base, token, "/api/folders") or []
    folder = next((f["uid"] for f in folders if f["title"] == FOLDER_TITLE), None)
    if folder is None:
        folder = _api(base, token, "/api/folders", {"title": FOLDER_TITLE})["uid"]

    sources = _api(base, token, "/api/datasources")
    ds = next((d["uid"] for d in sources
               if d["type"] == "prometheus" and d.get("isDefault")), None)
    if ds is None:
        sys.exit("no default prometheus datasource")

    rules = [build_rule(*r, folder, ds) for r in RULES]
    _api(base, token,
         f"/api/v1/provisioning/folder/{folder}/rule-groups/{GROUP}",
         {"title": GROUP, "folderUid": folder, "interval": INTERVAL_SECONDS,
          "rules": rules},
         method="PUT")

    ensure_contact_point(base, token)
    set_policy_tree(base, token)

    print(f"pushed {len(rules)} rules to {FOLDER_TITLE}/{GROUP} "
          f"(evaluated every {INTERVAL_SECONDS}s), notifying {CONTACT_EMAIL}")
    for r in rules:
        print(f"  {r['labels']['severity']:8} {r['for']:>4}  {r['title']}")


if __name__ == "__main__":
    main()
