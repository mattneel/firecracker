# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for a device passthrough API."""

import os
import re
from pathlib import Path

import pytest

from framework.artifacts import GUEST_KERNEL_DEFAULT, pin_guest_kernel, pin_pci

# Optional SBDF of the device to assign in the tests that need a real one. When it
# is not set, a device bound to vfio-pci with a free IOMMU group is looked for.
VFIO_SBDF_ENV = "FC_TEST_VFIO_SBDF"

PCI_DEVICES_PATH = Path("/sys/bus/pci/devices")
VFIO_PCI_DRIVER = "vfio-pci"


def _driver_of(device: Path):
    """Name of the driver bound to a PCI device, or None if it is unbound."""
    driver = device / "driver"
    return driver.resolve().name if driver.exists() else None


def _assignable_devices():
    """SBDFs of the PCI devices that can be assigned to a microVM.

    A device can be assigned when it is bound to vfio-pci and every device in its
    IOMMU group is either the device itself or also bound to vfio-pci, which is
    what the kernel requires for the group to be viable.
    """
    assignable = []
    for device in sorted(PCI_DEVICES_PATH.iterdir()):
        if _driver_of(device) != VFIO_PCI_DRIVER:
            continue

        group = device / "iommu_group"
        if not group.exists():
            continue

        members = (group.resolve() / "devices").iterdir()
        if all(
            member.name == device.name or _driver_of(member) == VFIO_PCI_DRIVER
            for member in members
        ):
            assignable.append(device.name)

    return assignable


def _device_ids(sbdf):
    """Vendor and device id (as `lspci -nn` prints them) of a PCI device."""
    device = PCI_DEVICES_PATH / sbdf
    # The sysfs attributes are hexadecimal with a `0x` prefix, `lspci` prints them
    # without it.
    vendor = int((device / "vendor").read_text().strip(), 16)
    device_id = int((device / "device").read_text().strip(), 16)
    return f"{vendor:04x}:{device_id:04x}"


@pytest.fixture
def vfio_device():
    """SBDF of a host device that can be assigned to a microVM."""
    assignable = _assignable_devices()
    requested = os.environ.get(VFIO_SBDF_ENV)
    if requested is not None:
        assert requested in assignable, (
            f"{VFIO_SBDF_ENV}={requested} is not assignable, the host provides "
            f"{assignable or 'no device bound to vfio-pci'}"
        )
        return requested

    if not assignable:
        pytest.skip("no PCI device bound to vfio-pci with a viable IOMMU group")

    return assignable[0]


@pin_pci(True)
@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_device_is_visible_in_guest(uvm, vfio_device):
    """
    Test that a real host device is assigned to the guest.

    Needs a device bound to vfio-pci (see `FC_TEST_VFIO_SBDF`), and is skipped on
    hosts that do not provide one.
    """
    expected_ids = _device_ids(vfio_device)

    vm = uvm
    vm.spawn()
    vm.basic_config()
    vm.api.device_passthrough.put(id="dev0", sbdf=vfio_device)
    vm.add_net_iface()
    vm.start()

    # The guest sees the device with the ids of the host device. The addresses of
    # its BARs are the ones the microVM assigned, so the guest is driving the
    # device through the emulated configuration space.
    lspci = vm.ssh.check_output("lspci -nn").stdout.lower()
    assert (
        f"[{expected_ids}]" in lspci
    ), f"device {expected_ids} not present in:\n{lspci}"

    # The MSI-X capability is the one Firecracker needs from the device, and it is
    # visible to the guest.
    detail = vm.ssh.check_output("lspci -nn -vv").stdout
    for block in detail.split("\n\n"):
        if expected_ids in block.lower():
            assert (
                "MSI-X" in block
            ), f"MSI-X capability missing for {expected_ids}:\n{block}"
            break
    else:
        raise AssertionError(f"no details for {expected_ids} in:\n{detail}")

    # The guest enumerated and read the device, and the microVM is still running.
    assert vm.api.describe.get().json()["state"] == "Running"


@pin_pci(True)
@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_api_device_passthrough(uvm):
    """
    Test device passthrough API commands.
    """

    vm = uvm
    vm.spawn()
    vm.basic_config()

    # Missing required field 'sbdf'
    expected_msg = re.escape("missing field `sbdf`")
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="dev0")

    # Valid passthrough device configs and overwrites
    vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")
    vm.api.device_passthrough.put(id="nvme0", sbdf="01:02.03")

    # Duplicate SBDF
    expected_msg = re.escape("Duplicate device passthrough SBDF")
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="nvme1", sbdf="01:02.03")

    # Adding a second device should be OK
    vm.api.device_passthrough.put(id="nvme1", sbdf="0000:01:02.04")

    # Empty id should fail
    expected_msg = re.escape("The ID cannot be empty.")
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="", sbdf="0000:01:02.05")


@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_api_device_passthrough_runtime(uvm):
    """
    Test device passthrough API commands during runtime.
    """

    vm = uvm
    vm.spawn()
    vm.basic_config()

    # Not a runtime API
    vm.start()
    expected_msg = re.escape(
        "The requested operation is not supported after starting the microVM"
    )
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="nvme69", sbdf="01:02.03")


@pin_pci(True)
@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_cannot_be_restored(
    uvm_booted, microvm_factory, guest_kernel, rootfs
):
    """
    Test that a snapshot cannot be restored with passthrough devices configured.

    The state of a passthrough device is not part of a snapshot, so the device
    would silently be missing from the guest.
    """
    snapshot = uvm_booted.snapshot_full()
    uvm_booted.kill()

    vm = microvm_factory.build(guest_kernel, rootfs, pci=True)
    vm.spawn()
    vm.basic_config()
    vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")

    expected_msg = re.escape("Passthrough devices cannot be restored from a snapshot")
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.restore_from_snapshot(snapshot)


@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_incompatible_devices_no_pci(
    microvm_factory, guest_kernel, rootfs
):
    """
    Test that adding device without PCI fails at API level.
    """
    vm = microvm_factory.build(guest_kernel, rootfs, pci=False)
    vm.jailer.setup()
    vm.spawn()
    vm.basic_config()

    expected_msg = re.escape("Passthrough devices attached, but PCI disabled")
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")


@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_incompatible_devices_dp_then_balloon(
    microvm_factory, guest_kernel, rootfs
):
    """
    Test that adding balloon after passthrough device fails at API level.
    """
    vm = microvm_factory.build(guest_kernel, rootfs, pci=True)
    vm.jailer.setup()
    vm.spawn()
    vm.basic_config()

    vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")
    expected_msg = re.escape(
        "Passthrough devices are not compatible with memory balloon device"
    )
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.balloon.put(
            amount_mib=0, deflate_on_oom=False, stats_polling_interval_s=1
        )


@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_incompatible_devices_balloon_then_dp(
    microvm_factory, guest_kernel, rootfs
):
    """
    Test that adding passthrough device after balloon fails at API level.
    """
    vm = microvm_factory.build(guest_kernel, rootfs, pci=True)
    vm.jailer.setup()
    vm.spawn()
    vm.basic_config()

    vm.api.balloon.put(amount_mib=0, deflate_on_oom=False, stats_polling_interval_s=1)
    expected_msg = re.escape(
        "Passthrough devices are not compatible with memory balloon device"
    )
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")


@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_incompatible_devices_dp_then_mem_hot_plug(
    microvm_factory, guest_kernel, rootfs
):
    """
    Test that adding memory hotplug after passthrough device fails at API level.
    """
    vm = microvm_factory.build(guest_kernel, rootfs, pci=True)
    vm.jailer.setup()
    vm.spawn()
    vm.basic_config()

    vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")
    expected_msg = re.escape(
        "Passthrough devices are not compatible with memory hot-plugging device"
    )
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.memory_hotplug.put(
            total_size_mib=256, slot_size_mib=256, block_size_mib=64
        )


@pin_guest_kernel(GUEST_KERNEL_DEFAULT)
def test_device_passthrough_incompatible_devices_mem_hot_plug_then_dp(
    microvm_factory, guest_kernel, rootfs
):
    """
    Test that adding passthrough device after memory hotplug fails at API level.
    """
    vm = microvm_factory.build(guest_kernel, rootfs, pci=True)
    vm.jailer.setup()
    vm.spawn()
    vm.basic_config()

    vm.api.memory_hotplug.put(total_size_mib=256, slot_size_mib=256, block_size_mib=64)
    expected_msg = re.escape(
        "Passthrough devices are not compatible with memory hot-plugging device"
    )
    with pytest.raises(RuntimeError, match=expected_msg):
        vm.api.device_passthrough.put(id="nvme0", sbdf="0000:01:02.03")
