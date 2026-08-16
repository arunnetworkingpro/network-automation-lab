# network

A small Ethernet data center, built in Cisco CML and driven entirely from code.

Nothing here is clicked together in the GUI. One YAML file describes the fabric;
scripts build it, configure it, and prove it works. Wipe the lab and it comes back
identical.

Phase 1 is a classic L2/L3 spine-leaf. Phase 2 layers BGP EVPN / VXLAN on the same
cabling, which is why the control plane already looks the way it does.

---

## The fabric

```
                    external connector ──── 192.168.2.0/24 (home LAN, internet)
                             │
                          [ jump ]  Ubuntu, two NICs
                             │ vlan 30
             ┌───────────────┴───────────────┐
          spine1 ●                         ● spine2      iol-xe    (L3 only)
             │  ╲                         ╱  │
             │    ╲                     ╱    │           4 × /31 links
             │      ╲                 ╱      │           full bipartite mesh
           leaf1 ●─────╲───────────╱─────● leaf2         ioll2-xe  (L2 + SVI)
             │                             │
         srv-app1                       srv-db1          alpine
          vlan 10                        vlan 20
```

Every leaf uplinks to **both** spines. No leaf-to-leaf link, no spine-to-spine link.
That is the defining property: any host is exactly two hops from any other, and
adding a leaf never touches an existing one.

Currently two leaves rather than four — the host has 6 cores, and cores are the
binding constraint, not RAM. `leaf3` and `leaf4` sit commented out in
`topology/dc-fabric.yml`; uncommenting them and re-running the build adds them
without disturbing anything already running. Demonstrating that is worth more than
running four leaves on day one.

## Addressing

| Purpose | Range | Notes |
|---|---|---|
| Loopbacks | `10.0.0.0/24` | spines `.1–.2`, leaves `.11–.12`. Router ID + BGP peering |
| P2P underlay | `10.1.0.0/16` | carved into `/31`s: `10.1.<spine>.<leaf×2>` |
| VLAN 10 — app | `10.10.10.0/24` | gateway `.1` |
| VLAN 20 — db | `10.10.20.0/24` | gateway `.1` |
| VLAN 30 — mgmt | `10.10.30.0/24` | jump server at `.10` |
| Home LAN | `192.168.2.0/24` | bridged external connector |

`/31` on point-to-point links, as production does — no wasted network and broadcast
addresses. The scheme is computed, never hand-written, so `10.1.2.5` reads as
"spine2, leaf3 side" on sight and the same link gets the same address on every
rebuild.

## Control plane

- **OSPF area 0** across the `/31`s and loopbacks. Its only job is making every
  loopback reachable from every other loopback.
- **iBGP, AS 65000.** Both spines are route reflectors; every leaf peers with both
  spine loopbacks. Leaves never peer with each other, which is what keeps adding a
  leaf an O(1) change.

OSPF alone would work at this size. The split exists because it is exactly what
Phase 2's EVPN expects — so Phase 2 becomes an addition rather than a rewrite.

---

## Layout

```
topology/dc-fabric.yml     single source of truth -- edit here, not in the GUI
topology/link-map.yml      GENERATED: which interface ended up carrying which address
templates/                 Jinja2: spine, leaf, jump (cloud-init), server
scripts/discover.py        inventory the controller before designing anything
scripts/build_topology.py  create the lab, nodes, links
scripts/gen_configs.py     render day-0 configs, optionally push them
scripts/bootstrap_ssh.py   generate SSH host keys over telnet
scripts/verify.py          the check ladder, run from the jump box
scripts/lab_up.py          bring the fabric back after a power cycle, unattended
scripts/lab_up_boot.sh     what @reboot cron runs: paths, logging, exit code
cml/client.py              authenticated CML client
docs/design.md             the design, the tradeoffs, and every gotcha found
docs/inventory.md          every device, address and cable, and how to reach it
```

`link-map.yml` matters more than it looks. It records the interface labels the
controller actually handed back, so config generation never assumes `Ethernet0/1`
means what we hoped it meant.

## Usage

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp example.env ~/.cml.env && chmod 600 ~/.cml.env    # then fill it in

.venv/bin/python scripts/discover.py                 # what can this controller do?
.venv/bin/python scripts/build_topology.py --start   # build it
.venv/bin/python scripts/gen_configs.py --push       # configure it
.venv/bin/python scripts/bootstrap_ssh.py            # SSH host keys
.venv/bin/python scripts/verify.py                   # prove it works
```

`gen_configs.py --push` wipes and reboots each node, because a day-0 config only
takes effect at boot. Scope it with `--only leaf1,leaf2` when you have changed one
thing. Re-run `bootstrap_ssh.py` afterwards — a wipe takes the SSH host key with it.

Credentials live only in `~/.cml.env`, chmod 600, never in this repo. Rendered
configs are gitignored: they embed the device password in cleartext.

### After a power cycle

CML does not auto-start labs — when the server reboots, every node comes back
STOPPED. `lab_up.py` waits for the controller, starts the lab, waits for the nodes,
and re-bootstraps any SSH host key that did not survive:

```bash
.venv/bin/python scripts/lab_up.py            # bring it up and wait
.venv/bin/python scripts/lab_up.py --check    # report state, change nothing
```

It is safe to run at any time — on an already-running fabric it does nothing and
exits 0, which is what makes it usable from `@reboot` cron. It is installed there
on the Pi, via a wrapper that keeps the absolute paths and logging out of the
crontab line:

```bash
@reboot /home/arun/Project/cml-datacenter/scripts/lab_up_boot.sh
tail -f ~/lab_up.log        # watch a boot, or read the last one
```

Nothing gates on the Pi and the CML server booting in any particular order: the
controller wait is 15 minutes of retries, so whichever comes up second is fine.

Two things measured while testing it, both of which shape how it waits. CML's
`STARTED` means only that the node process is running, so the readiness gate gates
on `BOOTED` alone — every node type here, external connector included, reaches it.
And `BOOTED` still is not the same as "IOS is answering": a leaf can report BOOTED
with neither SSH nor telnet up yet, so the host-key step is retried rather than
trusted on the first pass.

## Verification

`verify.py` runs a ladder from the jump box, where the whole fabric is visible. Each
rung depends on the one below, so the first failure names the broken layer:

```
1. jump addressing            did cloud-init apply?
2. jump -> leaf SVI           is the access port in the right VLAN?
3. jump -> loopbacks          has OSPF converged?
4. jump -> hosts              is BGP carrying the host subnets?
5. jump -> internet           does the bridge reach the modem?
```

Current state: **8 passed, 0 failed.**

---

## Things this build discovered about CML 2.9.1

Worth writing down, because all three looked like network faults and none were.
Every one was **config that applied cleanly and did nothing**.

**The L2 node is `ioll2-xe`**, not the `iol-xe-l2` you would reasonably guess. This
is the entire reason `discover.py` runs before the build.

**`alpine` is not cloud-init.** Its node definition declares an `alpine` generator
that reads `node.cfg` for a few known directives. `hostname` is honoured; arbitrary
shell — file writes, `ip` commands, `/etc/local.d` hooks — is silently ignored. The
node boots clean, as `localhost` on DHCP, with nothing in the console log to explain
why. The `ubuntu` node next door genuinely is cloud-init. Two hosts, two formats.
Resolved by handing out host addresses from DHCP pools on the leaves, narrowed to a
single usable address each — deterministic, and closer to real practice anyway.

**`ioll2-xe` has no EEM at all.** `show event manager policy registered` is rejected
as invalid input, and `event manager` lines in a startup-config are discarded without
error — they never even reach `show run`. IOS will not generate an RSA key from a
startup-config, so the spines use an EEM applet at boot and the leaves cannot.
`bootstrap_ssh.py` telnets in and does it, which is why the vty keeps
`transport input telnet ssh`: on a freshly wiped leaf with no host key, telnet is the
only way in.

The lesson worth keeping: when a device boots healthy but behaves as though
unconfigured, check whether the config was *accepted* before debugging the network.
`show run` is the fast test — if what you sent is not in there, nothing downstream of
it is real.

## Ansible

Inventory is generated, not written — `scripts/gen_inventory.py` reads the same
topology everything else does, so adding a leaf never means editing a host list.
No password is stored in it; `ansible_password` resolves from the environment at
run time, which keeps the inventory safe to read and paste.

```bash
./scripts/run_ansible.sh playbooks/validate.yml
./scripts/run_ansible.sh playbooks/validate.yml --limit leaves
```

`validate.yml` asserts *intent* rather than reachability: every OSPF adjacency FULL,
every iBGP session Established, the right neighbor count per role. A fabric can pass
ping while quietly running on one spine because the other never came up — that is
what this catches and `verify.py` cannot.

### Why Ansible runs on the jump box

CML's external connector is `protected` and `snooped`. It drops frames whose source
or destination is not the attached node's own address, which is exactly what routing
*through* the jump requires. Measured, not guessed:

| test | result |
|---|---|
| jump → Pi, source `192.168.2.50` | works |
| jump → Pi, source `10.10.30.10` | dropped |
| Pi → fabric, with a static route in place | dropped |
| leaf1 → Pi | dropped, though leaf1 → jump works |

A static route on the Pi (or the home router) is **necessary but not sufficient**;
clearing `protected` and `snooped` on the connector and restarting the node did not
lift it either. So `run_ansible.sh` syncs to the jump and runs there. The jump has
direct fabric access and a real path to the LAN, which is the whole reason it exists.

This also means the home-router static route alone will not give the phone and laptop
direct fabric access. Reaching the fabric means going through the jump — `ssh` to
`192.168.2.50` and work from there.

## Next

- Push device config with Ansible, replacing `gen_configs.py --push` for day-2 changes
- Phase 2: BGP EVPN / VXLAN on the same cabling — pending whether `iol-xe` supports it
