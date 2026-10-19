"""Virtual networking configuration.

Only user-mode (NAT) networking is enabled by default.  Bridged networking is
exposed only when the platform reports a working implementation; host-only
networking is not implemented yet and is rejected before QEMU is launched.
"""

from __future__ import annotations

import re

from openqvm.core.errors import UnsupportedFeatureError, ValidationError
from openqvm.core.models import NetworkMode, NicModel, NicSpec

_BRIDGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$")

SECURITY_NOTE_NAT = (
    "NAT networking isolates the guest behind QEMU's user-mode network stack. "
    "Explicitly forwarded ports are reachable from the host address you configure."
)
SECURITY_NOTE_BRIDGED = (
    "A bridged adapter places the guest directly on the physical network. The guest can reach other "
    "hosts and may be reachable by them. Use a dedicated bridge and firewall rules."
)


def build_netdev_arguments(nic: NicSpec, index: int) -> tuple[str, str]:
    """Return ``(netdev_argument, device_argument)`` for one adapter.

    Arguments are passed to QEMU as separate argv entries; no shell is involved.
    Values that end up inside a comma-separated QEMU option are validated and
    cannot contain commas or newlines.
    """

    netdev_id = f"net{index}"
    if nic.mode is NetworkMode.NAT:
        options = [f"user,id={netdev_id}"]
        for forward in nic.port_forwards:
            host = forward.host_address
            if not re.fullmatch(r"[0-9A-Fa-f:.]+", host):
                raise ValidationError(
                    f"Invalid host address for port forwarding: {host}",
                    hint="Use an IPv4 or IPv6 address without a port.",
                )
            options.append(
                f"hostfwd={forward.protocol}:{host}:{forward.host_port}-:{forward.guest_port}"
            )
        netdev = ",".join(options)
    elif nic.mode is NetworkMode.BRIDGED:
        bridge = (nic.bridge or "").strip()
        if not _BRIDGE_RE.match(bridge):
            raise ValidationError(
                "The bridge name is not valid.",
                hint="Use letters, digits, dots, dashes or underscores (maximum 32 characters).",
            )
        netdev = f"bridge,id={netdev_id},br={bridge}"
    else:
        raise UnsupportedFeatureError(
            "Host-only networking is not implemented yet.",
            hint="Use NAT networking or configure a host bridge and select bridged mode.",
        )

    device_options = [f"netdev={netdev_id}", f"mac={nic.mac}"]
    if nic.model is NicModel.VIRTIO:
        device = "virtio-net-pci," + ",".join(device_options)
    else:
        device = "e1000e," + ",".join(device_options)
    return netdev, device


def security_note(mode: NetworkMode) -> str:
    if mode is NetworkMode.BRIDGED:
        return SECURITY_NOTE_BRIDGED
    return SECURITY_NOTE_NAT


def default_port_forward() -> tuple[str, int, int, str]:
    return ("tcp", 2222, 22, "127.0.0.1")
