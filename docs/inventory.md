# Lab inventory

Everything in the lab, and how to get to it. The machine-readable version lives in
`ansible/inventory/generated_fabric.yml`, which is generated and gitignored — this
one is for you.

Lab: **dc-fabric-phase1** on CML at `192.168.2.41`.

## Getting in

Only the jump box is reachable from the house. Everything else is behind it.

```bash
ssh arun@192.168.2.50          # from your phone, laptop, or the Pi
```

From the jump, every device is one hop away. This is not a preference — CML's
external connector drops frames that are not addressed to or from the attached
node itself, so nothing can route *through* the jump. See the README.

**Password:** one credential for every device, generated and stored only in
`~/.cml.env` on the Pi:

```bash
grep LAB_DEVICE_PASS ~/.cml.env
```

## Devices

| device | address | image | role |
|---|---|---|---|
| `jump` | `192.168.2.50` (LAN) / `10.10.30.10` (fabric) | ubuntu | the way in; runs Ansible |
| `spine1` | `10.0.0.1` | `iol-xe` | underlay + BGP route reflector |
| `spine2` | `10.0.0.2` | `iol-xe` | underlay + BGP route reflector |
| `leaf1` | `10.0.0.11` | `ioll2-xe` | VLAN 10 + 30, SVIs, DHCP |
| `leaf2` | `10.0.0.12` | `ioll2-xe` | VLAN 20, SVI, DHCP |
| `srv-app1` | `10.10.10.11` | alpine | host in VLAN 10, on leaf1 |
| `srv-db1` | `10.10.20.11` | alpine | host in VLAN 20, on leaf2 |
| `ext` | — | external connector | bridge0 onto `192.168.2.0/24` |

Switch addresses are **loopbacks**, not interface addresses. They are reachable
because OSPF advertises them, which means a successful SSH is itself a small proof
the underlay works.

## Cabling

| link | spine side | leaf side |
|---|---|---|
| spine1 ↔ leaf1 | `Et0/0` `10.1.1.0` | `Et0/0` `10.1.1.1` |
| spine1 ↔ leaf2 | `Et1/0` `10.1.1.2` | `Et0/0` `10.1.1.3` |
| spine2 ↔ leaf1 | `Et0/0` `10.1.2.0` | `Et1/0` `10.1.2.1` |
| spine2 ↔ leaf2 | `Et1/0` `10.1.2.2` | `Et1/0` `10.1.2.3` |

Host ports: `srv-app1` on leaf1 `Et1/1` (vlan 10), `srv-db1` on leaf2 `Et1/1`
(vlan 20), `jump` on leaf1 `Et1/2` (vlan 30).

`/31` scheme is `10.1.<spine>.<leaf×2>`, spine takes the even address. So `10.1.2.3`
reads as "spine2, leaf2 side" without looking anything up.

## Worth running once you are in

On a spine:

```
show ip ospf neighbor      both leaves, FULL
show ip bgp summary        both leaves Established, prefixes received
```

On leaf1:

```
show ip route bgp          10.10.20.0/24 via BOTH spine loopbacks -- ECMP and
                           route reflection, visible in one command
show ip dhcp binding       srv-app1's lease
show vlan brief            vlans 10 and 30 with their access ports
```

## Health checks

```bash
.venv/bin/python scripts/verify.py                  # reachability, 8 checks
./scripts/run_ansible.sh playbooks/validate.yml     # protocol state, all devices
```

The first proves packets get there. The second proves the fabric is running the way
it was designed to — a lab can pass every ping while quietly running on one spine.
