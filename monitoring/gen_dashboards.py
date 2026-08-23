#!/usr/bin/env python3
"""Generate the Grafana dashboards for the CML lab.

Emits monitoring/dashboards/*.json, ready to import into Grafana Cloud via
Dashboards -> New -> Import. Regenerate after a topology change:

    .venv/bin/python monitoring/gen_dashboards.py

Why generated rather than hand-written JSON: the series->colour assignment has
to be stable per entity (a filter that drops leaf2 must not repaint spine1), and
Grafana expresses that as a per-series override block. Writing those by hand for
16 entities across two dashboards is where mistakes live.

Colours are the validated 8-slot categorical palette, dark-mode steps, in the
published order. That order IS the colourblind-safety mechanism -- it clears the
adjacent-pair gates (worst CVD dE 8.4, normal-vision 19.3). Do not re-order it,
and do not invent a 9th hue: fold extra entities into a table instead.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"

# Validated categorical palette, dark surface (#1a1a19), published slot order.
SERIES = [
    "#3987e5",  # 1 blue
    "#d95926",  # 2 orange
    "#199e70",  # 3 aqua
    "#c98500",  # 4 yellow
    "#d55181",  # 5 magenta
    "#008300",  # 6 green
    "#9085e9",  # 7 violet
    "#e66767",  # 8 red
]

# Status palette -- reserved. Never reused as a series colour.
GOOD, WARNING, SERIOUS, CRITICAL = "#0ca30c", "#fab219", "#ec835a", "#d03b3b"
MUTED = "#898781"  # axis/annotation ink, and the "this is a limit" reference line

DS = {"type": "prometheus", "uid": "${datasource}"}

# cml_compute_{memory,disk}_bytes carries the used/free/total split in a `state`
# label, so the two sides of the ratio differ by exactly that label. Without
# `ignoring(state)` the vector match finds no pairs and the panel is silently
# empty -- no error, just nothing.
MEM_USED_PCT = ('100 * cml_compute_memory_bytes{state="used"}'
                ' / ignoring(state) cml_compute_memory_bytes{state="total"}')
DISK_USED_PCT = ('100 * cml_compute_disk_bytes{state="used"}'
                 ' / ignoring(state) cml_compute_disk_bytes{state="total"}')


def _thresholds(steps):
    return {
        "mode": "absolute",
        "steps": [{"color": c, "value": v} for c, v in steps],
    }


def _target(expr, legend=None, ref="A", instant=False):
    t = {"datasource": DS, "expr": expr, "refId": ref, "editorMode": "code"}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t.update(instant=True, format="table", range=False)
    return t


def _color_overrides(mapping: dict[str, str], extra: dict[str, list] | None = None):
    """Pin each entity to its own colour, by name. Rank never decides colour."""
    out = []
    for name, hexcolor in mapping.items():
        props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexcolor}}]
        if extra and name in extra:
            props.extend(extra[name])
        out.append({"matcher": {"id": "byName", "options": name}, "properties": props})
    return out


def timeseries(title, targets, x, y, w, h, unit="short", overrides=None,
               desc=None, legend_calcs=None, soft_max=None, min_=0):
    """Line chart. 2px lines, no fill, recessive points, crosshair tooltip."""
    custom = {
        "drawStyle": "line",
        "lineWidth": 2,
        "fillOpacity": 0,
        "showPoints": "never",
        "pointSize": 8,
        "spanNulls": False,
        "axisBorderShow": False,
        "gradientMode": "none",
    }
    if soft_max is not None:
        custom["axisSoftMax"] = soft_max
    defaults = {"unit": unit, "custom": custom, "min": min_,
                "color": {"mode": "palette-classic"}}
    return {
        "type": "timeseries",
        "title": title,
        "description": desc or "",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            # A legend is mandatory once two series share the plot; identity is
            # never carried by colour alone.
            "legend": {
                "showLegend": True,
                "displayMode": "table" if legend_calcs else "list",
                "placement": "bottom",
                "calcs": legend_calcs or [],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def stat(title, expr, x, y, w, h, unit="short", steps=None, desc=None,
         decimals=None, text_size=48, graph="none"):
    return {
        "type": "stat",
        "title": title,
        "description": desc or "",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [_target(expr)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": _thresholds(steps or [(MUTED, None)]),
                "color": {"mode": "thresholds"},
                "mappings": [],
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "textMode": "auto",
            "colorMode": "value",
            "graphMode": graph,
            "justifyMode": "auto",
            "text": {"valueSize": text_size},
        },
    }


def gauge(title, expr, x, y, w, h, unit="percent", steps=None, desc=None, max_=100):
    """A single ratio against a limit -- a meter, not a two-slice pie."""
    return {
        "type": "gauge",
        "title": title,
        "description": desc or "",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [_target(expr)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "max": max_,
                "decimals": 1,
                "thresholds": _thresholds(steps or [(GOOD, None), (WARNING, 70), (CRITICAL, 90)]),
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        },
    }


def bargauge(title, expr, legend, x, y, w, h, unit="percent", steps=None,
             desc=None, max_=100):
    """Compare magnitude across entities -- sorted bars beat 8 overlapping lines."""
    return {
        "type": "bargauge",
        "title": title,
        "description": desc or "",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [_target(expr, legend)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "max": max_,
                "decimals": 1,
                "thresholds": _thresholds(steps or [(GOOD, None), (WARNING, 75), (CRITICAL, 90)]),
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "valueMode": "text",
            "showUnfilled": True,
            "sizing": "auto",
        },
    }


def table(title, targets, x, y, w, h, transformations, overrides=None,
          desc=None, sort_by=None):
    """One row per entity, one column per measure.

    Every target is an instant query reduced with `sum by (...)` so the frame
    carries only the labels that become columns -- joining raw series drags in
    job/instance/__name__ and the join field names stop being predictable.
    """
    return {
        "type": "table",
        "title": title,
        "description": desc or "",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "transformations": transformations,
        "fieldConfig": {
            "defaults": {
                # Values wear text ink; a status colour appears only where the
                # cell also spells the state out in words.
                "color": {"mode": "thresholds"},
                "thresholds": _thresholds([("text", None)]),
                "custom": {
                    "align": "auto",
                    "cellOptions": {"type": "auto"},
                    "filterable": True,
                    "inspect": False,
                },
            },
            "overrides": overrides or [],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"], "fields": ""},
            "sortBy": [{"displayName": sort_by, "desc": False}] if sort_by else [],
        },
    }


def state_timeline(title, targets, x, y, w, h, mappings, desc=None,
                   overrides=None):
    """Discrete state over time. Entity names sit on the y axis, so identity is
    direct-labelled and colour only ever carries the state."""
    return {
        "type": "state-timeline",
        "title": title,
        "description": desc or "",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": _thresholds([(CRITICAL, None), (GOOD, 1)]),
                "mappings": mappings,
                "min": 0,
                "max": 1,
                "custom": {
                    "lineWidth": 2,  # the 2px surface gap between adjacent fills
                    "fillOpacity": 90,
                    "spanNulls": False,
                    "insertNulls": False,
                    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
                },
            },
            "overrides": overrides or [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "never",
            "alignValue": "center",
            "rowHeight": 0.9,
            "perPage": 20,
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


def row(title, y, collapsed=False, panels=None):
    return {
        "type": "row",
        "title": title,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "collapsed": collapsed,
        "panels": panels or [],
    }


def dashboard(title, uid, panels, description, variables=None, tags=None):
    tpl = [{
        "type": "datasource",
        "name": "datasource",
        "label": "Data source",
        "query": "prometheus",
        "refresh": 1,
        "hide": 0,
        "current": {},
        "options": [],
    }]
    tpl.extend(variables or [])
    return {
        "title": title,
        "uid": uid,
        "description": description,
        "tags": tags or ["cml", "lab"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "graphTooltip": 1,  # shared crosshair across every panel
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",  # matches the 60s scrape; faster just redraws stale points
        "templating": {"list": tpl},
        "panels": panels,
    }


# --------------------------------------------------------------- capacity ----

def capacity_dashboard():
    p = []

    p.append(stat(
        "CPU oversubscription", "cml_compute_cpu_predicted / cml_compute_cpu_count",
        0, 0, 6, 5, unit="none", decimals=1, text_size=56, graph="area",
        desc=("Cores CML predicts the running nodes need, divided by cores the box "
              "actually has. Above 1.0 means the fabric is riding on overcommit."),
        steps=[(GOOD, None), (WARNING, 1.0), (SERIOUS, 2.0), (CRITICAL, 3.0)],
    ))
    p.append(stat(
        "Physical cores", "cml_compute_cpu_count", 6, 0, 6, 5, unit="none",
        desc="What the i5-8500T actually has. The hard ceiling.",
    ))
    p.append(stat(
        "vCPUs allocated", "cml_compute_allocated_cpus", 12, 0, 6, 5, unit="none",
        desc="vCPUs handed to running nodes. Exceeding physical cores is normal for IOL.",
        steps=[(GOOD, None), (WARNING, 7), (CRITICAL, 13)],
    ))
    p.append(stat(
        "Nodes running", 'cml_compute_nodes{state="running"}', 18, 0, 6, 5,
        unit="none", desc="Running nodes across all labs on this compute.",
    ))

    p.append(timeseries(
        "CPU demand vs physical capacity",
        [
            _target("cml_compute_cpu_predicted", "Predicted need", "A"),
            _target("cml_compute_allocated_cpus", "Allocated", "B"),
            _target("cml_compute_cpu_count", "Physical cores", "C"),
        ],
        0, 5, 16, 9, unit="none", legend_calcs=["lastNotNull", "max"],
        desc=("Physical cores is a limit, not a series -- it is drawn grey and dashed "
              "so the two real measures read against it."),
        overrides=_color_overrides(
            {"Predicted need": SERIES[0], "Allocated": SERIES[1], "Physical cores": MUTED},
            extra={"Physical cores": [
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
                {"id": "custom.hideFrom", "value": {"legend": False, "tooltip": False, "viz": False}},
            ]},
        ),
    ))
    p.append(gauge(
        "Compute CPU", "cml_compute_cpu_percent", 16, 5, 8, 5,
        desc="Actual utilisation. Usually low -- IOL nodes idle cheaply; it is the "
             "boot storm that hurts.",
    ))
    p.append(gauge(
        "Compute memory used", MEM_USED_PCT, 16, 10, 8, 4,
        desc="20.5 GB total. Memory has never been the constraint here -- cores are.",
    ))

    p.append(timeseries(
        "Load average",
        [_target("cml_compute_load", "{{window}}")],
        0, 14, 12, 7, unit="none", legend_calcs=["lastNotNull", "max"],
        desc="Compare against 6 cores: sustained load above 6 means real contention.",
        overrides=_color_overrides(
            {"1m": SERIES[0], "5m": SERIES[1], "15m": SERIES[2]}),
    ))
    p.append(timeseries(
        "Compute memory",
        [
            _target('cml_compute_memory_bytes{state="used"}', "Used", "A"),
            _target('cml_compute_memory_bytes{state="total"}', "Total", "B"),
        ],
        12, 14, 12, 7, unit="bytes", legend_calcs=["lastNotNull"],
        overrides=_color_overrides(
            {"Used": SERIES[0], "Total": MUTED},
            extra={"Total": [{"id": "custom.lineStyle",
                              "value": {"fill": "dash", "dash": [10, 10]}}]},
        ),
    ))

    p.append(row("Raspberry Pi (the automation jump box)", 21, collapsed=True, panels=[
        gauge("Pi CPU",
              '100 - (avg(rate(node_cpu_seconds_total{job="pi",mode="idle"}[5m])) * 100)',
              0, 22, 6, 6),
        gauge("Pi temperature", 'max(node_thermal_zone_temp{job="pi"})',
              6, 22, 6, 6, unit="celsius", max_=90,
              steps=[(GOOD, None), (WARNING, 65), (CRITICAL, 80)],
              desc="Throttling starts around 80 C on a Pi 5."),
        gauge("Pi memory used",
              '100 * (1 - node_memory_MemAvailable_bytes{job="pi"} / node_memory_MemTotal_bytes{job="pi"})',
              12, 22, 6, 6),
        gauge("Pi disk used (/)",
              '100 - 100 * node_filesystem_avail_bytes{job="pi",mountpoint="/"} '
              '/ node_filesystem_size_bytes{job="pi",mountpoint="/"}',
              18, 22, 6, 6),
    ]))

    return dashboard(
        "CML Lab - Capacity", "cml-lab-capacity", p,
        description=("Whether the lab has the headroom to run what is on it. The lead "
                     "metric is oversubscription: six physical cores carrying a fabric "
                     "CML sizes at far more."),
    )


# ----------------------------------------------------------------- fabric ----

# Fixed slot order. Colour follows the entity; adding leaf3 appends, never shuffles.
NODE_ORDER = ["spine1", "spine2", "leaf1", "leaf2", "srv-app1", "srv-db1", "jump", "ext"]
LINK_ORDER = [
    "spine1-Ethernet0/0<->leaf1-Ethernet0/0",
    "spine1-Ethernet1/0<->leaf2-Ethernet0/0",
    "spine2-Ethernet0/0<->leaf1-Ethernet1/0",
    "spine2-Ethernet1/0<->leaf2-Ethernet1/0",
    "srv-app1-eth0<->leaf1-Ethernet1/1",
    "srv-db1-eth0<->leaf2-Ethernet1/1",
    "jump-ens2<->leaf1-Ethernet1/2",
    "jump-ens3<->ext-port",
]

NODE_COLORS = dict(zip(NODE_ORDER, SERIES))
LINK_COLORS = dict(zip(LINK_ORDER, SERIES))


def fabric_dashboard():
    lab = '{lab="$lab"}'
    p = []

    p.append(stat(
        "Nodes booted", f'sum(cml_node_booted{lab})', 0, 0, 6, 4, unit="none",
        desc="Nodes that reached BOOTED. Compare against the node count in the table below.",
        steps=[(CRITICAL, None), (GOOD, 8)], text_size=44,
    ))
    p.append(stat(
        "Links up", f'sum(cml_link_up{lab})', 6, 0, 6, 4, unit="none",
        steps=[(CRITICAL, None), (GOOD, 8)], text_size=44,
    ))
    p.append(stat(
        "Fabric throughput", f'sum(rate(cml_link_read_bytes_total{lab}[5m])) * 8',
        12, 0, 6, 4, unit="bps", text_size=44,
        desc="Sum of read rate across every link, as bits per second.",
    ))
    p.append(stat(
        "Dropped packets (1h)", f'sum(increase(cml_link_drops_total{lab}[1h]))',
        18, 0, 6, 4, unit="none", text_size=44,
        desc="Any sustained non-zero here means the virtual wire is losing frames.",
        steps=[(GOOD, None), (WARNING, 1), (CRITICAL, 100)],
    ))

    p.append(timeseries(
        "Per-node CPU", [_target(f"cml_node_cpu_percent{lab}", "{{node}}")],
        0, 4, 16, 9, unit="percent", legend_calcs=["lastNotNull", "max"],
        desc="IOL nodes idle near 1%. Sustained climbs mean a control-plane loop.",
        overrides=_color_overrides(NODE_COLORS),
    ))
    p.append(bargauge(
        "Per-node RAM used", f"cml_node_ram_percent{lab}", "{{node}}",
        16, 4, 8, 9,
        desc="Percent of each node's own allocation, not of the host.",
    ))

    p.append(timeseries(
        "Per-link throughput",
        [_target(f"(rate(cml_link_read_bytes_total{lab}[5m]) "
                 f"+ rate(cml_link_write_bytes_total{lab}[5m])) * 8", "{{link}}")],
        0, 13, 24, 9, unit="bps", legend_calcs=["lastNotNull", "max"],
        desc="Both directions summed, in bits per second, from CML's link counters.",
        overrides=_color_overrides(LINK_COLORS),
    ))

    p.append(timeseries(
        "Node uptime", [_target(f"cml_node_uptime_seconds{lab}", "{{node}}")],
        0, 22, 12, 8, unit="s", legend_calcs=["lastNotNull"],
        desc="A sawtooth here is a node that keeps restarting.",
        overrides=_color_overrides(NODE_COLORS),
    ))
    p.append(timeseries(
        "Packet drops per link",
        [_target(f"rate(cml_link_drops_total{lab}[5m])", "{{link}}")],
        12, 22, 12, 8, unit="pps", legend_calcs=["lastNotNull", "max"],
        overrides=_color_overrides(LINK_COLORS),
    ))

    lab_var = {
        "type": "query",
        "name": "lab",
        "label": "Lab",
        "datasource": DS,
        "query": "label_values(cml_lab_state_info, lab)",
        "refresh": 1,
        "includeAll": False,
        "multi": False,
        "current": {},
        "options": [],
    }
    return dashboard(
        "CML Lab - Fabric", "cml-lab-fabric", p,
        description="Per-node and per-link health for the spine-leaf fabric.",
        variables=[lab_var],
    )


# --------------------------------------------------------------- overview ----

# Columns Grafana invents while joining instant frames. Each query contributes a
# Time field; only the join key and the value columns are wanted.
_JOIN_JUNK = {f"Time {i}": True for i in range(1, 8)} | {"Time": True}


def _col(name, **props):
    """A per-column override, addressed by the name the organize step gave it."""
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k.replace("__", "."), "value": v}
                           for k, v in props.items()]}


def _pct_cell(warn=75, crit=90):
    """A percent column drawn as an in-cell meter rather than a bare number."""
    return {
        "unit": "percent",
        "min": 0,
        "max": 100,
        "decimals": 1,
        "custom__cellOptions": {"type": "gauge", "mode": "gradient"},
        "thresholds": _thresholds([(GOOD, None), (WARNING, warn), (CRITICAL, crit)]),
    }


NODE_STATE_MAP = [{"type": "value", "options": {
    "BOOTED":          {"text": "BOOTED",   "color": GOOD,     "index": 0},
    "STARTED":         {"text": "STARTED",  "color": GOOD,     "index": 1},
    "BOOTING":         {"text": "BOOTING",  "color": WARNING,  "index": 2},
    "QUEUED":          {"text": "QUEUED",   "color": WARNING,  "index": 3},
    "STOPPED":         {"text": "STOPPED",  "color": MUTED,    "index": 4},
    "DEFINED_ON_CORE": {"text": "DEFINED",  "color": MUTED,    "index": 5},
}}]

UP_DOWN_MAP = [{"type": "value", "options": {
    "0": {"text": "DOWN", "color": CRITICAL, "index": 0},
    "1": {"text": "UP",   "color": GOOD,     "index": 1},
}}]


def overview_dashboard():
    """Everything about the fabric on one page: inventory, state history,
    throughput, errors, capacity, and the Pi carrying it all."""
    lab = '{lab="$lab"}'
    p = []

    # ---- at a glance -------------------------------------------------------
    p.append(row("At a glance", 0))
    # topk(1, ...) not a bare match: over a 6h window a lab that was stopped and
    # restarted has both a STOPPED and a STARTED series with a last-non-null of
    # 1, and the tile renders two state names side by side.
    p.append(stat("Lab state", f"topk(1, cml_lab_state_info{lab} == 1)", 0, 1, 3, 4,
                  unit="none", text_size=28,
                  desc="The lab's own state as CML reports it."))
    p[-1]["targets"][0]["legendFormat"] = "{{state}}"
    p[-1]["options"]["textMode"] = "name"
    p[-1]["fieldConfig"]["defaults"]["thresholds"] = _thresholds([(GOOD, None)])

    p.append(stat("Nodes booted", f"sum(cml_node_booted{lab})", 3, 1, 3, 4,
                  unit="none", text_size=40,
                  steps=[(CRITICAL, None), (GOOD, 8)],
                  desc="Nodes that reached BOOTED, out of 8 in phase 1."))
    p.append(stat("Links up", f"sum(cml_link_up{lab})", 6, 1, 3, 4, unit="none",
                  text_size=40, steps=[(CRITICAL, None), (GOOD, 8)]))
    p.append(stat("Links down",
                  f"count(cml_link_up{lab}) - sum(cml_link_up{lab})",
                  9, 1, 3, 4, unit="none", text_size=40,
                  steps=[(GOOD, None), (CRITICAL, 1)],
                  desc="Anything above zero is a broken adjacency."))
    p.append(stat("CPU oversubscription",
                  "cml_compute_cpu_predicted / cml_compute_cpu_count",
                  12, 1, 3, 4, unit="none", decimals=1, text_size=40, graph="area",
                  steps=[(GOOD, None), (WARNING, 1.0), (SERIOUS, 2.0), (CRITICAL, 3.0)],
                  desc="Cores CML thinks the running nodes need, over the six the "
                       "box actually has."))
    p.append(stat("Fabric throughput",
                  f"sum(rate(cml_link_read_bytes_total{lab}[5m])) * 8",
                  15, 1, 3, 4, unit="bps", text_size=32))
    p.append(stat("Drops (1h)", f"sum(increase(cml_link_drops_total{lab}[1h]))",
                  18, 1, 3, 4, unit="none", text_size=40,
                  steps=[(GOOD, None), (WARNING, 1), (CRITICAL, 100)],
                  desc="Frames the virtual wire lost in the last hour."))
    p.append(stat("CML API", "cml_up", 21, 1, 3, 4, unit="none", text_size=28,
                  steps=[(CRITICAL, None), (GOOD, 1)],
                  desc="Whether the exporter could reach the CML API on its last scrape."))
    p[-1]["fieldConfig"]["defaults"]["mappings"] = UP_DOWN_MAP

    # ---- nodes -------------------------------------------------------------
    p.append(row("Nodes", 5))
    p.append(table(
        "Node inventory",
        [
            _target(f"sum by (node, kind, state) (cml_node_state_info{lab})", ref="A", instant=True),
            _target(f"sum by (node) (cml_node_cpu_percent{lab})", ref="B", instant=True),
            _target(f"sum by (node) (cml_node_ram_percent{lab})", ref="C", instant=True),
            _target(f"sum by (node) (cml_node_ram_allocated_bytes{lab})", ref="D", instant=True),
            _target(f"sum by (node) (cml_node_uptime_seconds{lab})", ref="E", instant=True),
        ],
        0, 6, 24, 10,
        desc="Every node in the lab, with what it is, what state it is in, and what "
             "it is consuming right now.",
        sort_by="Node",
        transformations=[
            {"id": "joinByField", "options": {"byField": "node", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": _JOIN_JUNK | {"Value #A": True},
                "renameByName": {
                    "node": "Node", "kind": "Kind", "state": "State",
                    "Value #B": "CPU %", "Value #C": "RAM %",
                    "Value #D": "RAM allocated", "Value #E": "Uptime",
                },
                "indexByName": {"node": 0, "kind": 1, "state": 2, "Value #B": 3,
                                "Value #C": 4, "Value #D": 5, "Value #E": 6},
            }},
        ],
        overrides=[
            _col("State", mappings=NODE_STATE_MAP,
                 custom__cellOptions={"type": "color-text"}),
            _col("CPU %", **_pct_cell(warn=60, crit=85)),
            _col("RAM %", **_pct_cell()),
            _col("RAM allocated", unit="bytes"),
            _col("Uptime", unit="s"),
        ],
    ))

    p.append(state_timeline(
        "Node state history",
        [_target(f"cml_node_booted{lab}", "{{node}}")],
        0, 16, 24, 8, UP_DOWN_MAP,
        desc="A red band is a node that was not BOOTED. Read it left to right to see "
             "exactly when a reload happened.",
    ))

    p.append(timeseries(
        "Per-node CPU", [_target(f"cml_node_cpu_percent{lab}", "{{node}}")],
        0, 24, 12, 9, unit="percent", legend_calcs=["lastNotNull", "max"],
        desc="IOL nodes idle near 1%. A sustained climb is a control-plane loop.",
        overrides=_color_overrides(NODE_COLORS),
    ))
    p.append(timeseries(
        "Per-node RAM", [_target(f"cml_node_ram_percent{lab}", "{{node}}")],
        12, 24, 12, 9, unit="percent", legend_calcs=["lastNotNull", "max"],
        desc="Percent of each node's own allocation, not of the host.",
        overrides=_color_overrides(NODE_COLORS),
    ))

    # ---- links -------------------------------------------------------------
    p.append(row("Links", 33))
    p.append(table(
        "Link inventory",
        [
            _target(f"sum by (link, node_a, node_b) (cml_link_up{lab})", ref="A", instant=True),
            _target(f"sum by (link) (rate(cml_link_read_bytes_total{lab}[5m])) * 8", ref="B", instant=True),
            _target(f"sum by (link) (rate(cml_link_write_bytes_total{lab}[5m])) * 8", ref="C", instant=True),
            _target(f"sum by (link) (rate(cml_link_drops_total{lab}[5m]))", ref="D", instant=True),
            _target(f"sum by (link) (increase(cml_link_drops_total{lab}[1h]))", ref="E", instant=True),
        ],
        0, 34, 24, 10,
        desc="Every virtual wire, its endpoints, both directions of traffic, and its "
             "error rate.",
        sort_by="Link",
        transformations=[
            {"id": "joinByField", "options": {"byField": "link", "mode": "outer"}},
            {"id": "organize", "options": {
                "excludeByName": _JOIN_JUNK,
                "renameByName": {
                    "link": "Link", "node_a": "A side", "node_b": "B side",
                    "Value #A": "Status", "Value #B": "Rx", "Value #C": "Tx",
                    "Value #D": "Drops/s", "Value #E": "Drops (1h)",
                },
                "indexByName": {"link": 0, "node_a": 1, "node_b": 2, "Value #A": 3,
                                "Value #B": 4, "Value #C": 5, "Value #D": 6,
                                "Value #E": 7},
            }},
        ],
        overrides=[
            _col("Status", mappings=UP_DOWN_MAP,
                 custom__cellOptions={"type": "color-text"}),
            _col("Rx", unit="bps", decimals=0),
            _col("Tx", unit="bps", decimals=0),
            _col("Drops/s", unit="pps", decimals=2,
                 thresholds=_thresholds([(GOOD, None), (WARNING, 0.01), (CRITICAL, 1)]),
                 custom__cellOptions={"type": "color-text"}),
            _col("Drops (1h)", unit="none", decimals=0,
                 thresholds=_thresholds([(GOOD, None), (WARNING, 1), (CRITICAL, 100)]),
                 custom__cellOptions={"type": "color-text"}),
        ],
    ))

    p.append(state_timeline(
        "Link state history",
        [_target(f"cml_link_up{lab}", "{{link}}")],
        0, 44, 24, 9, UP_DOWN_MAP,
        desc="Flapping shows up here as stripes. A steady green bar per link is what "
             "a healthy fabric looks like.",
    ))

    p.append(timeseries(
        "Per-link throughput",
        [_target(f"(rate(cml_link_read_bytes_total{lab}[5m]) "
                 f"+ rate(cml_link_write_bytes_total{lab}[5m])) * 8", "{{link}}")],
        0, 53, 12, 9, unit="bps", legend_calcs=["lastNotNull", "max"],
        desc="Both directions summed, from CML's own link counters.",
        overrides=_color_overrides(LINK_COLORS),
    ))
    p.append(timeseries(
        "Packet drops per link",
        [_target(f"rate(cml_link_drops_total{lab}[5m])", "{{link}}")],
        12, 53, 12, 9, unit="pps", legend_calcs=["lastNotNull", "max"],
        desc="The error-rate view. Flat on zero is correct; anything else is a "
             "starved vSwitch.",
        overrides=_color_overrides(LINK_COLORS),
    ))

    # ---- capacity ----------------------------------------------------------
    p.append(row("Capacity on the compute", 62))
    p.append(gauge("Compute CPU", "cml_compute_cpu_percent", 0, 63, 6, 6,
                   desc="Actual utilisation. It is the boot storm that hurts, not "
                        "steady state."))
    p.append(gauge("Compute memory used", MEM_USED_PCT, 6, 63, 6, 6,
                   desc="20.5 GB total. Memory has never been the constraint "
                        "here -- cores are."))
    p.append(gauge("Compute disk used", DISK_USED_PCT, 12, 63, 6, 6,
                   steps=[(GOOD, None), (WARNING, 80), (CRITICAL, 92)]))
    p.append(stat("Nodes running on compute", 'cml_compute_nodes{state="running"}',
                  18, 63, 6, 6, unit="none",
                  desc="Across every lab on this compute, not just the selected one."))

    p.append(timeseries(
        "CPU demand vs physical capacity",
        [
            _target("cml_compute_cpu_predicted", "Predicted need", "A"),
            _target("cml_compute_allocated_cpus", "Allocated", "B"),
            _target("cml_compute_cpu_count", "Physical cores", "C"),
        ],
        0, 69, 12, 9, unit="none", legend_calcs=["lastNotNull", "max"],
        desc="Physical cores is a limit, not a series -- grey and dashed, so the two "
             "real measures read against it.",
        overrides=_color_overrides(
            {"Predicted need": SERIES[0], "Allocated": SERIES[1],
             "Physical cores": MUTED},
            extra={"Physical cores": [
                {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
            ]},
        ),
    ))
    p.append(timeseries(
        "Load average", [_target("cml_compute_load", "{{window}}")],
        12, 69, 12, 9, unit="none", legend_calcs=["lastNotNull", "max"],
        desc="Compare against 6 cores: sustained load above 6 is real contention.",
        overrides=_color_overrides(
            {"1m": SERIES[0], "5m": SERIES[1], "15m": SERIES[2]}),
    ))

    # ---- the Pi ------------------------------------------------------------
    p.append(row("Raspberry Pi (the box doing the monitoring)", 78, collapsed=True,
                 panels=[
        gauge("Pi CPU",
              '100 - (avg(rate(node_cpu_seconds_total{job="pi",mode="idle"}[5m])) * 100)',
              0, 79, 6, 6),
        gauge("Pi temperature", 'max(node_thermal_zone_temp{job="pi"})',
              6, 79, 6, 6, unit="celsius", max_=90,
              steps=[(GOOD, None), (WARNING, 65), (CRITICAL, 80)],
              desc="Throttling starts around 80 C. This is the closest thing to a "
                   "power reading the lab has -- the nodes are virtual, so there is "
                   "no PSU or PDU to measure."),
        gauge("Pi memory used",
              '100 * (1 - node_memory_MemAvailable_bytes{job="pi"} '
              '/ node_memory_MemTotal_bytes{job="pi"})',
              12, 79, 6, 6),
        gauge("Pi disk used (/)",
              '100 - 100 * node_filesystem_avail_bytes{job="pi",mountpoint="/"} '
              '/ node_filesystem_size_bytes{job="pi",mountpoint="/"}',
              18, 79, 6, 6),
        timeseries("Pi temperature over time",
                   [_target('node_thermal_zone_temp{job="pi"}', "{{type}}")],
                   0, 85, 12, 8, unit="celsius", min_=None,
                   legend_calcs=["lastNotNull", "max"],
                   overrides=_color_overrides({"cpu-thermal": SERIES[0]})),
        timeseries("Pi network errors",
                   [
                       _target('rate(node_network_receive_errs_total{job="pi"}[5m])',
                               "{{device}} rx", "A"),
                       _target('rate(node_network_transmit_errs_total{job="pi"}[5m])',
                               "{{device}} tx", "B"),
                   ],
                   12, 85, 12, 8, unit="pps", legend_calcs=["lastNotNull", "max"],
                   desc="Errors on the Pi's own NIC. Flat zero means the metrics "
                        "pipeline itself is not the problem."),
    ]))

    lab_var = {
        "type": "query",
        "name": "lab",
        "label": "Lab",
        "datasource": DS,
        "query": "label_values(cml_lab_state_info, lab)",
        "refresh": 1,
        "includeAll": False,
        "multi": False,
        "current": {},
        "options": [],
    }
    return dashboard(
        "CML Lab - Command Center", "cml-lab-overview", p,
        description=("Single-pane view of the spine-leaf lab: node inventory and state "
                     "history, link status and error rates, compute headroom, and the "
                     "Pi that scrapes it all."),
        variables=[lab_var],
        tags=["cml", "lab", "overview"],
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("cml-capacity", capacity_dashboard),
                        ("cml-fabric", fabric_dashboard),
                        ("cml-overview", overview_dashboard)):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(build(), indent=2) + "\n")
        print(f"wrote {path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
