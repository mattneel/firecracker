# PCI Device Passthrough (VFIO) [Developer Preview]

> [!WARNING]
>
> This feature is currently in
> [Developer Preview](RELEASE_POLICY.md#developer-preview-features). It may have
> limitations, and its API or behavior may change in future releases.

Device passthrough lets a guest own a physical PCIe device, such as a GPU,
directly. Firecracker maps the device into the guest through VFIO, so the guest
driver talks to the hardware without the VMM in the data path.

## Prerequisites

Host requirements:

- An IOMMU enabled and in a mode that supports assignment. For Intel hosts, boot
  with `intel_iommu=on iommu=pt`; for AMD hosts, `amd_iommu=on iommu=pt`. Use
  `dmesg | grep -i iommu` to confirm that the IOMMU is enabled.

- The device must be bound to the `vfio-pci` driver and not used by any host
  driver. For example:

  ```bash
  driverctl set-override 0000:01:00.0 vfio-pci
  ```

  Check the result with `lspci -nnk -s 01:00.0`, which should list
  `Kernel driver in use: vfio-pci`.

- VFIO assigns whole IOMMU groups, so every device in the group of the
  passthrough device must be available for assignment as well. Use
  `readlink -f /sys/bus/pci/devices/0000:01:00.0/iommu_group` and the group
  contents to check.

- The device has to expose MSI-X interrupts. Devices without an MSI-X
  capability, or with zero MSI-X vectors, are rejected.

- Every memory BAR of the device has to be at least as large as the host page
  size. I/O BARs are ignored.

- The microVM has to run with the PCI transport enabled, which is what the
  `--enable-pci` command line flag turns on. Passthrough devices are rejected
  when the PCI transport is disabled.

When Firecracker runs under the jailer, the VFIO character devices
(`/dev/vfio/vfio` and one node per IOMMU group) and the sysfs folder of every
host device that is bound to the VFIO driver are made available inside the jail
automatically, so no additional option is needed. Running without the jailer
requires access to the same paths, which normally means running with the
privileges to open them.

Guest requirements:

- A kernel with PCI support and a driver for the device. A passed through GPU
  shows up in the guest like any other PCIe device and is driven by the regular
  driver of the guest distribution.

## Usage

Passthrough devices are configured before the microVM is started, either through
the API or through a configuration file. Each device is identified by the
segment, bus, device and function of the host device (`SBDF`).

```console
socket_location=/run/firecracker.socket

curl --unix-socket $socket_location -i \
    -X PUT 'http://localhost/device-passthrough/gpu0' \
    -H 'Accept: application/json' \
    -H 'Content-Type: application/json' \
    -d '{
        "sbdf": "0000:01:00.0"
    }'
```

The same configuration can be passed to a Firecracker started with
`--enable-pci --config-file`:

```json
{
  "device-passthrough": [
    { "id": "gpu0", "sbdf": "0000:01:00.0" }
  ]
}
```

Devices are reset when they are attached to the microVM, so a device that was
used by a previous deployment is not exposed with the state it was left in. They
are reset again when the microVM is torn down.

## Limitations

- **No memory overcommitment.** All guest memory is mapped into the IOMMU so
  that the device can perform DMA, which pins it in the host. For this reason
  balloon devices and memory hot-plugging are rejected in configurations with
  passthrough devices.
- **No snapshot support.** The state of a physical device is not visible to the
  VMM, so snapshot creation is rejected while a passthrough device is attached.
- **No hot-plugging.** Devices can only be attached before boot.
- **MSI-X only.** Legacy INTx interrupts are not supported.
- **No BAR relocation.** The guest has to keep the BAR addresses that the
  microVM assigns at boot. Drivers that reassign BARs can no longer reach the
  device.
- **One PCI segment and bus.** Devices share the single PCI bus of the microVM.

## Security

A passed through device can access the entire guest memory by design. Treat it
as part of the trusted computing base of the microVM: a device with malicious
firmware can read and write guest memory, and can keep DMA access to a guest
memory page after the guest has released it, until the mapping is removed when
the microVM is torn down.

Because of this, only assign devices that you trust with the contents of the
microVM, and do not use passthrough and untrusted guests interchangeably with
microVMs that do not use it.
