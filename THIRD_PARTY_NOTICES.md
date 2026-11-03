# Dependency and third-party notices

The root [LICENSE](LICENSE) covers the original example integration and its
original neural graph. It does not relicense dependencies, their hardware, or
the materials linked below. Install SDKs separately from authorized sources.

## Artificial Brains SDK and products

`artificialbrains_sdk` is a separate dependency, not vendored in this repository.
Its upstream package declares a proprietary license. The controlling terms are
[the SDK LICENSE](https://github.com/artificial-brains-inc/artificial_brains/blob/main/LICENSE).
The SDK, engine, compiler, Device Manager, device firmware, bitstream and hardware
remain outside this example license. Public Python imports do not grant rights
to redistribute their implementations. The linked *Introductory Guide to
Building an Artificial Brain* is a separately published reference, not a relicensed
copy of the SDK or underlying technology.

## Aero Hand Open / Chestnut Robotics

Aero Hand Open is the robotic peripheral used by this example. Its software,
including `aero-open-sdk`, is supplied under Apache-2.0; hardware design files
and documentation have separate terms. See the vendor's
[licensing guide](https://github.com/Chestnut-Robotics/aero-hand-open/blob/main/LICENSE.md)
and [SDK project](https://github.com/Chestnut-Robotics/aero-hand-open/tree/main/sdk).
Nothing in this example grants manufacturing rights or changes those licenses.

Attribution: Aero Hand Open, Copyright (c) 2025 Chestnut Robotics, Inc.

The integration calls the separately installed SDK. The named preset angles
identified as `origin: vendor` in `poses.json` come from the vendor's software
examples. The geometry fixtures in `tests/test_open_reference.py` adapt the
published software's linear geometry equations and constants solely for tests.
`ControlledAeroHand` in `aerohand_control/serial_open.py` initializes the pinned
vendor class's state with a project-specific serial-opening sequence.
To the extent those portions are derived from the upstream software, they
remain subject to **Apache-2.0**, included in [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt).
The example license does not narrow the rights in those upstream portions.
The fixture wrappers, test scenarios, integration, and neural graph are project
contributions; no vendor engine, CAD, board design or firmware image is included.

Source references are retained in `poses.json` and the relevant source comments.
The example adapts the interface, pose orientation and test representation; it
does not claim those are official vendor modifications or certified results.

## Serial opening reference

The reset-free serial opening sequence is informed by Espressif's published
[esp-idf-monitor serial reader](https://github.com/espressif/esp-idf-monitor/blob/master/esp_idf_monitor/base/serial_reader.py).
The example uses pySerial directly and does not bundle the monitor. Any
identifiable upstream software portions remain under their upstream terms.

## Other separately installed dependencies

`pyserial`, `httpx`, `cryptography`, `esptool`, pytest, Playwright, PlatformIO,
Arduino/STM32 framework components and their transitive dependencies retain
their respective licenses. They are not included as source distributions here.
Use the license files in the installed packages for the exact versions obtained.
The included acquisition sketch is original example code; importing Arduino
headers does not transfer ownership of the framework or processor hardware.

## Distribution boundary

This repository supplies public application-level code and neural declarations.
It excludes `.abp` artifacts, SDK implementations, private construction formulas,
calibrated compiler internals, hardware schematics/CAD, commercial firmware,
credentials and device enrollment data. A local build may generate artifacts
subject to separate Artificial Brains product terms; do not infer redistribution
rights in their proprietary contents from this example's license.
