# DC fabric — Phase 1 design

Classic L2/L3 spine-leaf. Everything here is deliberately conventional; the goal is
to learn the patterns real data centers use, then layer EVPN/VXLAN on the same
cabling in Phase 2 without re-cabling anything.

## Topology

```
                 external_connector  ──── 192.168.2.0/24 (home LAN)
                          │
                       [ jump ]  Ubuntu, 2 NICs
                          │ vlan 30
        ┌─────────────────┴─────────────────┐
     spine1 ●───────────────────────────● spine2      iol-xe   (L3 only)
       ││││ \  \                 /  /  ││││
       ││││  \  \               /  /   ││││           full bipartite mesh
       ││││   \  \             /  /    ││││           8 × /31 links
     leaf1   leaf2          leaf3   leaf4             iol-xe-l2 (L2 + SVI)
       │       │              │       │
   srv-app1 srv-app2      srv-db1  srv-db2            alpine
    vlan10   vlan10        vlan20   vlan20
```

12 nodes. Every leaf uplinks to **both** spines — no leaf-to-leaf links, no
spine-to-spine link. That is the defining property of the design: any server is
exactly two hops from any other, and adding a leaf never touches existing leaves.

## Addressing

| Purpose | Range | Notes |
|---|---|---|
| Loopbacks | `10.0.0.0/24` | spines `.1-.2`, leaves `.11-.14`. Router ID + BGP peering. |
| P2P underlay | `10.1.0.0/16` | carved into `/31`s, one per spine-leaf link |
| VLAN 10 (app) | `10.10.10.0/24` | gateway `.1` |
| VLAN 20 (db) | `10.10.20.0/24` | gateway `.1` |
| VLAN 30 (mgmt) | `10.10.30.0/24` | jump server lives here at `.10` |
| Home LAN | `192.168.2.0/24` | via bridged external connector |

`/31` on point-to-point links rather than `/30` — same reason production does it:
no wasted network/broadcast addresses, and it forces you to get comfortable with
the notation.

## Control plane

- **OSPF area 0** across all `/31` links and loopbacks. This is the *underlay* —
  its only job is making every loopback reachable from every other loopback.
- **iBGP, AS 65000.** Both spines are route reflectors; every leaf is a client
  peering to both spine loopbacks. Leaves do not peer with each other.

Why both protocols when OSPF alone would work at this size: because that split —
IGP for infrastructure reachability, BGP for everything else — is exactly what
Phase 2's EVPN needs. Building it now means Phase 2 is an addition, not a rewrite.

## The lab does not stop at the CML boundary

Arun's **phone and personal laptop are lab participants**, not spectators. They sit
on `192.168.2.0/24`, and the bridged external connector puts the jump server on that
same wire — so from the fabric's point of view they are simply hosts that live one
hop beyond the jump box.

That is a deliberate design property, and it costs one thing to make real: a
**static route on the home router**.

```
10.0.0.0/8  via  192.168.2.50     # the fabric, reachable through jump
```

Without it, the phone can ssh to `192.168.2.50` and work *from* the jump box, but it
cannot reach `10.10.10.11` directly -- it has no idea where `10.10.0.0/16` lives and
sends it to the default gateway, which drops it. With the route, every device on the
home LAN reaches every server in the fabric by its real address. iOS in particular
cannot hold per-device static routes, so pushing this to the router is the only way
the phone gets there.

Three pieces have to line up, and all three are ours:

1. **Home router** — the static route above. The one manual step outside code.
2. **Jump server** — `net.ipv4.ip_forward=1`, plus routes into the fabric via the
   leaf1 SVI at `10.10.30.1`. Generated as part of its cloud-init.
3. **The fabric** — must know how to send return traffic back to `192.168.2.0/24`,
   otherwise pings leave and never come home. A default route on the leaves pointing
   at the jump's fabric address covers it.

Return path is the half people forget. A ping that arrives and cannot answer looks
exactly like a ping that never arrived.

## The external connector, and one decision for you

The jump server gets two interfaces:

1. **Fabric side** — VLAN 30, `10.10.30.10/24`. How Ansible reaches every switch.
2. **Bridged side** — external connector in `bridged` mode, landing directly on
   `192.168.2.0/24`, picking up DHCP from your home router.

That second one is what lets you `ssh arun@192.168.2.x` into the jump box from your
laptop and be inside the lab.

**Tradeoff worth understanding before we build it:** `bridged` puts the jump server
*directly on your home network* — it gets a real 192.168.2.x lease, your other
devices can reach it, and it can reach them. That is exactly what makes it
convenient, and also means a misconfigured lab node could leak traffic onto your
LAN. The main real-world risk is a lab device running a rogue DHCP server and
handing out bad leases to your actual home devices.

**DECIDED (15 Aug 2026): bridged, with a static address.** `192.168.2.50/24`, gateway
`192.168.2.1`, verified unused before assignment. Inbound SSH from the phone keeps
working and no lease can move the box out from under us.

Two things make this defensible rather than reckless:

- The controller reports `bridge0` as **`protected: true`**, which filters exactly the
  failure mode that matters here — a lab node running a rogue DHCP server and handing
  bad leases to real devices on the home network.
- A static address means the lab never asks the home router for anything.

Rejected: **NAT** (`virbr0`) — safest isolation, but no inbound path, which would make
the phone and laptop spectators instead of lab participants. **Bridged + DHCP** — one
less step today, but an address that moves is an address you cannot put in an SSH
config or an Ansible inventory.

## Resource budget

| Nodes | Count | RAM each | Total |
|---|---|---|---|
| spines (`iol-xe`) | 2 | ~512 MB | 1.0 GB |
| leaves (`iol-xe-l2`) | 4 | ~768 MB | 3.0 GB |
| jump (`ubuntu`) | 1 | 2 GB | 2.0 GB |
| servers (`alpine`) | 4 | 512 MB | 2.0 GB |
| external connector | 1 | 0 | 0 |
| **total** | **12** | | **~8 GB** |

Against a 64 GB host this is nothing. **CPU is the binding constraint, not RAM** —
6 cores. IOL nodes are Linux processes rather than QEMU VMs, which is the whole
reason this fits comfortably; the same fabric on NX-OSv 9000 would cost 8 GB *per
switch* and peg all 6 cores.

## Open questions — resolved during build, 15 Aug 2026

1. **Node-definition ids.** `iol-xe` is right; the L2 node is **`ioll2-xe`**, not the
   `iol-xe-l2` you would reasonably guess. Only `node_defs` changed. This is the
   entire justification for running discovery before the build.
2. **Routed ports on the L2 node.** Yes — `no switchport` works on `ioll2-xe`, so the
   `/31` uplinks are genuine routed ports. The SVI workaround was not needed.
3. **Host config format.** The two host types do *not* share a mechanism:
   - `ubuntu` → real cloud-init (`user-data`). Netplan via `write_files` works.
   - `alpine` → **not cloud-init.** A `node.cfg` read by a generator that honours a
     few known directives (`hostname`, `USERNAME`, `PASSWORD`). Arbitrary shell —
     file writes, `ip` commands, `/etc/local.d` hooks — is silently ignored. The node
     boots clean with none of it applied and nothing in the console log to explain it.

   Resolution: stop configuring the host. The leaves run DHCP pools narrowed to a
   single usable address each, so the servers get deterministic IPs from the network.
   Closer to real practice than baking addresses into hosts anyway.
4. **BGP EVPN on `iol-xe`** — still open. Decides whether Phase 2 stays light or forces
   NX-OSv and a much smaller fabric.

5. **SSH host keys — resolved, and the two images differ again.** IOS will not generate
   an RSA key from a startup-config; it is an exec-mode action. On the spines an EEM
   applet does it at boot. On the leaves that is impossible: **`ioll2-xe` has no EEM
   at all** — `show event manager policy registered` is invalid input, and
   `event manager` lines in a startup-config are *silently discarded*, never appearing
   in `show run`. Config that applies without error and does nothing.

   The image is `adventerprisek9`, so crypto is present; it simply cannot self-start.
   `scripts/bootstrap_ssh.py` telnets in and generates the key, which is why the vty
   keeps `transport input telnet ssh`. Re-run it after any `--push`, since a wipe takes
   the key with it. It skips devices already listening on 22.

### Still outstanding

- **Static route on the home router** — `10.0.0.0/8 via 192.168.2.50`. Until it
  exists, the phone and laptop can reach the jump but not the fabric behind it.
- **Phase 2** — whether `iol-xe` supports BGP EVPN (question 4 above).

### A note on debugging this build

Two of the three real problems were the same shape: **config that applied cleanly and
did nothing.** Alpine ignoring everything but `hostname`; the leaves discarding the EEM
block. Neither logged an error. Both looked like network faults and were not.

The lesson worth keeping: when a device boots healthy but behaves as if unconfigured,
check whether the config was *accepted* before debugging the network. `show run` is the
fast test — if what you sent is not in there, nothing downstream of it is real.
