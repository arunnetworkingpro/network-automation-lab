"""Deterministic host MACs, so DHCP can reserve by MAC.

CML assigns a fresh MAC at deploy time unless the interface has one configured,
so a wipe can change it. With the Phase 2b anycast gateway every leaf that has a
VLAN's SVI also hears every DHCP broadcast in it (BUM floods over the L2VNI), so a
narrowed address pool is no longer deterministic -- whichever leaf answers first
wins. Reservations are, but only if the MAC is fixed. Deriving it from the host's
IP means nothing new to keep in sync:

    10.10.10.12 -> 52:54:00:0a:0a:0c    (52:54:00 is the KVM/QEMU OUI CML uses)
"""

from __future__ import annotations

from netaddr import IPNetwork

OUI = (0x52, 0x54, 0x00)


def host_mac(ip: str) -> str:
    """Colon form, for CML's interface.mac_address."""
    octets = IPNetwork(ip).ip.words[1:]
    return ":".join(f"{b:02x}" for b in (*OUI, *octets))


def dhcp_client_id(ip: str) -> str:
    """IOS 'client-identifier' form: type 01 (Ethernet) + MAC, dotted in fours.

    BusyBox udhcpc (the alpine image) sends option 61 by default, and IOS matches
    a manual binding on the client-id whenever one is present -- so a binding
    keyed on 'hardware-address' would silently never match.
    """
    raw = "01" + host_mac(ip).replace(":", "")
    return ".".join(raw[i:i + 4] for i in range(0, len(raw), 4))
