#!/usr/bin/env python3
"""Prometheus exporter for CML 2.9.1.

Turns the CML REST API into metrics. The point of this lab is that six physical
cores have to carry a fabric CML thinks needs sixteen, so the headline metrics
are cml_compute_cpu_predicted vs cml_compute_cpu_count vs allocated_cpus.

Deliberately never reads a node's `configuration` field: rendered configs embed
the device password in cleartext (see .gitignore). Everything here comes from
lab_element_state, layer3_addresses, simulation_stats and links -- all secret-free.

Binds to localhost only. Grafana Alloy scrapes it and ships it onward.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cml.client import connect  # noqa: E402

log = logging.getLogger("cml_exporter")

# Node/link names and lab titles change only when we rebuild the topology, so
# fetching them on every scrape is wasted API calls. States and counters are
# always fresh; only this naming layer is cached.
TOPOLOGY_TTL = 300


class CMLCollector:
    """Scrapes CML on demand. One instance, reused across scrapes."""

    def __init__(self) -> None:
        self._client = None
        self._topology: dict[str, dict] = {}
        self._topology_at = 0.0

    # -- connection -------------------------------------------------------

    @property
    def session(self):
        if self._client is None:
            log.info("authenticating to CML")
            self._client = connect()
        return self._client._session

    def _get(self, endpoint: str):
        """GET an endpoint, re-authenticating once if the token has expired."""
        try:
            return self.session.get(endpoint).json()
        except Exception as exc:
            log.warning("%s failed (%s); reconnecting", endpoint, exc)
            self._client = None
            return self.session.get(endpoint).json()

    # -- naming layer -----------------------------------------------------

    def _topology_for(self, lab_id: str) -> dict:
        """Node id -> (name, kind) and link id -> (label, node_a, node_b), cached."""
        if time.monotonic() - self._topology_at > TOPOLOGY_TTL:
            self._topology = {}
            self._topology_at = time.monotonic()
        if lab_id in self._topology:
            return self._topology[lab_id]

        # exclude_configurations keeps the cleartext device secret out of the
        # response entirely; we still get label and node_definition.
        nodes = self._get(f"labs/{lab_id}/nodes?data=true&exclude_configurations=true")
        names = {n["id"]: n.get("label", n["id"][:8]) for n in nodes}
        kinds = {n["id"]: n.get("node_definition", "unknown") for n in nodes}
        links = {}
        for link_id in self._get(f"labs/{lab_id}/links"):
            d = self._get(f"labs/{lab_id}/links/{link_id}")
            links[link_id] = {
                "label": d.get("label", link_id[:8]),
                "node_a": names.get(d.get("node_a"), "?"),
                "node_b": names.get(d.get("node_b"), "?"),
            }

        self._topology[lab_id] = {"nodes": names, "kinds": kinds, "links": links}
        return self._topology[lab_id]

    # -- collection -------------------------------------------------------

    def collect(self):
        started = time.monotonic()
        up = GaugeMetricFamily("cml_up", "1 if the CML API answered this scrape")
        try:
            yield from self._collect_system()
            yield from self._collect_labs()
            up.add_metric([], 1)
        except Exception as exc:
            log.error("scrape failed: %s", exc)
            self._client = None
            up.add_metric([], 0)
        yield up

        dur = GaugeMetricFamily(
            "cml_scrape_duration_seconds", "Time spent collecting from the CML API"
        )
        dur.add_metric([], time.monotonic() - started)
        yield dur

    def _collect_system(self):
        stats = self._get("system_stats")
        health = self._get("system_health")
        info = self._get("system_information")

        labels = ["compute", "hostname"]
        cpu_count = GaugeMetricFamily(
            "cml_compute_cpu_count", "Physical cores the compute actually has", labels=labels
        )
        cpu_pred = GaugeMetricFamily(
            "cml_compute_cpu_predicted",
            "Cores CML predicts the running nodes need. Above cpu_count means oversubscribed.",
            labels=labels,
        )
        cpu_alloc = GaugeMetricFamily(
            "cml_compute_allocated_cpus", "vCPUs handed out to running nodes", labels=labels
        )
        cpu_pct = GaugeMetricFamily(
            "cml_compute_cpu_percent", "Compute CPU utilisation, percent", labels=labels
        )
        load = GaugeMetricFamily(
            "cml_compute_load", "Unix load average", labels=labels + ["window"]
        )
        mem = GaugeMetricFamily(
            "cml_compute_memory_bytes", "Compute memory", labels=labels + ["state"]
        )
        disk = GaugeMetricFamily(
            "cml_compute_disk_bytes", "Compute disk", labels=labels + ["state"]
        )
        mem_alloc = GaugeMetricFamily(
            "cml_compute_allocated_memory_bytes", "Memory handed out to running nodes", labels=labels
        )
        nodes_g = GaugeMetricFamily(
            "cml_compute_nodes", "Nodes on this compute", labels=labels + ["state"]
        )
        healthy = GaugeMetricFamily(
            "cml_compute_healthy", "1 if CML reports this compute valid", labels=labels
        )

        for cid, compute in stats.get("computes", {}).items():
            host = compute.get("hostname", "?")
            lv = [cid, host]
            s = compute.get("stats", {})

            cpu = s.get("cpu", {})
            cpu_count.add_metric(lv, cpu.get("count", 0))
            cpu_pred.add_metric(lv, cpu.get("predicted", 0))
            cpu_pct.add_metric(lv, cpu.get("percent", 0))
            for window, value in zip(("1m", "5m", "15m"), cpu.get("load", [])):
                load.add_metric(lv + [window], value)

            for state, value in s.get("memory", {}).items():
                mem.add_metric(lv + [state], value)
            for state, value in s.get("disk", {}).items():
                disk.add_metric(lv + [state], value)

            dom = s.get("dominfo", {})
            cpu_alloc.add_metric(lv, dom.get("allocated_cpus", 0))
            # CML reports allocated_memory in KiB here, unlike memory.* which is bytes.
            mem_alloc.add_metric(lv, dom.get("allocated_memory", 0) * 1024)
            nodes_g.add_metric(lv + ["total"], dom.get("total_nodes", 0))
            nodes_g.add_metric(lv + ["running"], dom.get("running_nodes", 0))

            hc = health.get("computes", {}).get(cid, {})
            healthy.add_metric(lv, 1 if hc.get("valid") else 0)

        yield from (
            cpu_count, cpu_pred, cpu_alloc, cpu_pct, load,
            mem, disk, mem_alloc, nodes_g, healthy,
        )

        licensed = GaugeMetricFamily("cml_licensed", "1 if CML reports a valid licence")
        licensed.add_metric([], 1 if health.get("is_licensed") else 0)
        yield licensed

        version = GaugeMetricFamily(
            "cml_version_info", "CML version, as a label", labels=["version"]
        )
        version.add_metric([info.get("version", "unknown")], 1)
        yield version

    def _collect_labs(self):
        lab_state = GaugeMetricFamily(
            "cml_lab_state_info", "1 for the lab's current state", labels=["lab", "state"]
        )
        nl = ["lab", "node", "kind"]
        node_state = GaugeMetricFamily(
            "cml_node_state_info", "1 for the node's current state", labels=nl + ["state"]
        )
        node_booted = GaugeMetricFamily(
            "cml_node_booted", "1 if the node reached BOOTED", labels=nl
        )
        node_cpu = GaugeMetricFamily(
            "cml_node_cpu_percent", "Per-node CPU usage, percent", labels=nl
        )
        node_ram_pct = GaugeMetricFamily(
            "cml_node_ram_percent", "Per-node RAM usage, percent of its allocation", labels=nl
        )
        node_ram = GaugeMetricFamily(
            "cml_node_ram_allocated_bytes", "RAM allocated to the node", labels=nl
        )
        node_uptime = GaugeMetricFamily(
            "cml_node_uptime_seconds", "Seconds the node has been BOOTED", labels=nl
        )

        ll = ["lab", "link", "node_a", "node_b"]
        link_up = GaugeMetricFamily("cml_link_up", "1 if the link is STARTED", labels=ll)
        rx_b = CounterMetricFamily("cml_link_read_bytes", "Bytes read on the link", labels=ll)
        tx_b = CounterMetricFamily("cml_link_write_bytes", "Bytes written on the link", labels=ll)
        rx_p = CounterMetricFamily("cml_link_read_packets", "Packets read on the link", labels=ll)
        tx_p = CounterMetricFamily("cml_link_write_packets", "Packets written on the link", labels=ll)
        drops = CounterMetricFamily("cml_link_drops", "Packets dropped on the link", labels=ll)

        for lab_id in self._get("labs"):
            meta = self._get(f"labs/{lab_id}")
            title = meta.get("lab_title", lab_id[:8])
            lab_state.add_metric([title, meta.get("state", "UNKNOWN")], 1)

            topo = self._topology_for(lab_id)
            names, kinds, link_meta = topo["nodes"], topo["kinds"], topo["links"]

            elements = self._get(f"labs/{lab_id}/lab_element_state")
            sim = self._get(f"labs/{lab_id}/simulation_stats")

            for nid, state in elements.get("nodes", {}).items():
                nv = [title, names.get(nid, nid[:8]), kinds.get(nid, "unknown")]
                node_state.add_metric(nv + [state], 1)
                node_booted.add_metric(nv, 1 if state == "BOOTED" else 0)

                st = sim.get("nodes", {}).get(nid, {})
                if not st:
                    continue
                node_cpu.add_metric(nv, st.get("cpu_usage", 0))
                node_ram_pct.add_metric(nv, st.get("ram_usage", 0))
                node_ram.add_metric(nv, st.get("ram", 0) * 1024 * 1024)
                node_uptime.add_metric(nv, st.get("times", {}).get("BOOTED", 0))

            for lid, counters in sim.get("links", {}).items():
                m = link_meta.get(lid, {})
                lv = [title, m.get("label", lid[:8]), m.get("node_a", "?"), m.get("node_b", "?")]
                rx_b.add_metric(lv, counters.get("readbytes", 0))
                tx_b.add_metric(lv, counters.get("writebytes", 0))
                rx_p.add_metric(lv, counters.get("readpackets", 0))
                tx_p.add_metric(lv, counters.get("writepackets", 0))
                drops.add_metric(lv, counters.get("drops", 0))
                link_up.add_metric(lv, 1 if elements.get("links", {}).get(lid) == "STARTED" else 0)

        yield from (
            lab_state, node_state, node_booted, node_cpu,
            node_ram_pct, node_ram, node_uptime,
            link_up, rx_b, tx_b, rx_p, tx_p, drops,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=9639)
    ap.add_argument("--host", default="127.0.0.1", help="localhost only by default")
    ap.add_argument("--once", action="store_true", help="print one scrape and exit")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    # One line per CML API call is unreadable at a 30s scrape interval.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    collector = CMLCollector()
    REGISTRY.register(collector)

    if args.once:
        from prometheus_client import generate_latest

        sys.stdout.write(generate_latest(REGISTRY).decode())
        return

    start_http_server(args.port, addr=args.host)
    log.info("serving metrics on http://%s:%d/metrics", args.host, args.port)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
