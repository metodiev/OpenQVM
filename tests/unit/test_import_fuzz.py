"""Property-based hardening tests for untrusted input.

The import format, the settings file and VM configurations are the three places
where OpenQVM reads attacker-controlled data.  These tests assert the contract the
rest of the code relies on: parsing either succeeds or raises an
:class:`OpenQVMError`, and a rejected package never leaves a VM or staging
directory behind.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from openqvm.core.errors import OpenQVMError, ValidationError
from openqvm.core.models import VMSpec
from openqvm.core.paths import AppPaths
from openqvm.storage.disks import DiskManager
from openqvm.storage.transfer import VmTransferService
from openqvm.store import AppSettings, VmStore

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=40),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=10), children, max_size=3),
    max_leaves=8,
)

_settings = hypothesis_settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def _service(tmp_path: Path) -> tuple[VmTransferService, AppPaths, VmStore]:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    store = VmStore(paths)
    return VmTransferService(paths, store, DiskManager(None)), paths, store


def _assert_nothing_registered(paths: AppPaths, store: VmStore) -> None:
    assert store.list_specs() == []
    assert list(paths.vms_dir.glob(".import-*")) == []
    assert list(paths.vms_dir.glob("*/vm.json")) == []


@given(payload=st.binary(max_size=4096))
@_settings
def test_arbitrary_package_bytes_are_rejected(tmp_path, payload):
    service, paths, store = _service(tmp_path)
    package = tmp_path / "payload.oqv"
    package.write_bytes(payload)

    with pytest.raises(OpenQVMError):
        service.import_vm(package)

    _assert_nothing_registered(paths, store)


@given(
    names=st.lists(st.text(max_size=40), max_size=4),
    data=st.binary(max_size=64),
)
@_settings
def test_archives_with_unexpected_entries_are_rejected(tmp_path, names, data):
    service, paths, store = _service(tmp_path)
    package = tmp_path / "archive.oqv"
    with zipfile.ZipFile(package, "w") as archive:
        for index, name in enumerate(names):
            archive.writestr(f"{index}-{name}", data)

    with pytest.raises(OpenQVMError):
        service.import_vm(package)

    _assert_nothing_registered(paths, store)


@given(
    vm=json_values,
    disks=st.one_of(st.none(), st.lists(json_values, max_size=2)),
    snapshots=st.one_of(st.none(), st.lists(json_values, max_size=2)),
    firmware=st.one_of(st.none(), st.lists(json_values, max_size=2)),
    version=st.sampled_from([1, 2, "1", None, True]),
)
@_settings
def test_manifest_fields_are_validated(tmp_path, vm, disks, snapshots, firmware, version):
    service, paths, store = _service(tmp_path)
    manifest: dict = {"format": "openqvm-export", "format_version": version, "vm": vm}
    for key, value in (("disks", disks), ("snapshots", snapshots), ("firmware", firmware)):
        if value is not None:
            manifest[key] = value
    package = tmp_path / "manifest.oqv"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    with pytest.raises(OpenQVMError):
        service.import_vm(package)

    _assert_nothing_registered(paths, store)


@given(value=json_values)
@_settings
def test_settings_parsing_never_leaks_unexpected_errors(value):
    try:
        parsed = AppSettings.from_dict(value)
    except ValidationError:
        return
    assert isinstance(parsed, AppSettings)
    assert AppSettings.from_dict(parsed.to_dict()) == parsed


@given(value=json_values)
@_settings
def test_vm_configuration_parsing_never_leaks_unexpected_errors(value):
    try:
        spec = VMSpec.from_dict(value)
    except ValidationError:
        return
    assert isinstance(spec, VMSpec)
    assert VMSpec.from_dict(spec.to_dict()) == spec
