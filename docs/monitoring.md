# Monitoring

Grafana Cloud renders the dashboards; nothing is self-hosted except two small
processes on the Pi. Metrics flow one way, outbound only:

```
CML 2.9.1 (192.168.2.41)
      | HTTPS, read-only REST
      v
cml_exporter.py  --127.0.0.1:9639-->  Grafana Alloy  --HTTPS remote_write-->  Grafana Cloud
                                            ^
                                            +-- node_exporter (the Pi itself)
```

Nothing listens on a routable address. The exporter binds loopback, Alloy's UI
binds loopback, and the only outbound path is Alloy pushing to Grafana Cloud.

## Why an exporter at all

CML has no Prometheus endpoint. It does have a REST API that reports exactly the
thing this lab is built around: `system_stats` returns `cpu.count` (what the
i5-8500T has), `cpu.predicted` (what CML thinks the running nodes need) and
`dominfo.allocated_cpus` (what it handed out). Six cores carrying a fabric CML
sizes at two to four times that is the defining constraint of the project, and
`cml_compute_cpu_predicted / cml_compute_cpu_count` puts a number on it.

### The one rule the exporter must not break

A node's `configuration` field contains the rendered device config, and those
embed the device password in cleartext -- which is why `configs/` is gitignored.
The exporter therefore **never reads a node payload without
`exclude_configurations=true`**. Every endpoint it touches (`system_stats`,
`system_health`, `lab_element_state`, `simulation_stats`, `links`) is
secret-free by construction.

If you extend the exporter, keep that property and re-check it:

```bash
curl -s localhost:9639/metrics | grep -ci 'secret\|password'   # must print 0
```

## Metrics

| Family | Notes |
|---|---|
| `cml_compute_cpu_{count,predicted,percent}` | the oversubscription story |
| `cml_compute_allocated_cpus` | vCPUs handed to running nodes |
| `cml_compute_{memory,disk}_bytes{state=}` | `total` / `used` / `free` |
| `cml_compute_load{window=}` | `1m` / `5m` / `15m` |
| `cml_node_{cpu,ram}_percent` | per node, labelled `node` and `kind` |
| `cml_node_{booted,uptime_seconds,state_info}` | per node |
| `cml_link_{read,write}_bytes_total` | counters -- always `rate()` them |
| `cml_link_{drops_total,up}` | per link, labelled with both endpoints |
| `cml_up`, `cml_scrape_duration_seconds` | exporter health |

Roughly 134 CML series plus 281 from the Pi. Grafana Cloud's free tier allows
10,000 active series, so there is room for several more phases.

Both scrapes run at **60s on purpose**: the free tier is sized at one data point
per minute, and a lab gains nothing from finer resolution.

## Running it

Both are user services -- no root anywhere.

```bash
systemctl --user status cml-exporter alloy
journalctl --user -u cml-exporter -f
```

To install from scratch on a fresh Pi:

```bash
cp monitoring/cml-exporter.service monitoring/alloy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cml-exporter alloy
```

User services stop at logout unless lingering is on. This needs root, once:

```bash
sudo loginctl enable-linger arun
```

## Credentials

`~/.cml.env` holds the CML login, `~/.grafana.env` the Grafana Cloud push
credentials. Both are `chmod 600`, both live outside the repo, and `*.env` is
gitignored so neither can be committed by accident. `config.alloy` reads the
Grafana values through `sys.env()`, so the config itself carries no secrets and
is safe to commit.

## Dashboards

`monitoring/dashboards/*.json` are generated, not hand-written:

```bash
.venv/bin/python monitoring/gen_dashboards.py
```

Import them in Grafana with **Dashboards -> New -> Import -> Upload JSON**, then
pick the Prometheus data source when prompted.

- **CML Lab - Capacity** -- oversubscription, compute CPU/memory/load, Pi health
- **CML Lab - Fabric** -- per-node CPU/RAM, per-link throughput, drops, uptime

Series colours come from a colourblind-safe eight-slot palette and are **pinned
per entity** in the generator: `spine1` is the same blue whether or not `leaf2`
is on screen. The slot order is the safety mechanism -- it clears the
adjacent-pair CVD gates -- so extend `NODE_ORDER` / `LINK_ORDER` by appending.
Never re-order, and never add a ninth colour; past eight entities, fold the tail
into a table.
