"""Shared test fixtures.

GUI tests use Qt's offscreen platform so they can run without a display server.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from openqvm.core.defaults import recommended_defaults  # noqa: E402
from openqvm.core.factory import create_vm_spec  # noqa: E402
from openqvm.core.models import GuestOS  # noqa: E402
from openqvm.core.paths import AppPaths  # noqa: E402
from openqvm.core.validation import HostResources  # noqa: E402
from openqvm.store import VmStore  # noqa: E402


@pytest.fixture
def host() -> HostResources:
    return HostResources(cpu_count=8, memory_total_mib=16384)


@pytest.fixture
def paths(tmp_path) -> AppPaths:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    return paths


@pytest.fixture
def store(paths) -> VmStore:
    return VmStore(paths)


@pytest.fixture
def linux_spec(host) -> object:
    return create_vm_spec(name="Test Linux", guest_os=GuestOS.LINUX, host=host)


def make_spec(host: HostResources, name: str = "Test VM", guest_os: GuestOS = GuestOS.LINUX):
    return create_vm_spec(name=name, guest_os=guest_os, host=host)


def default_disk_bytes(host: HostResources, guest_os: GuestOS = GuestOS.LINUX) -> int:
    return recommended_defaults(guest_os, host).disk_bytes
