"""Resolve an explicitly selected AeroHand USB identity without opening ports."""

from __future__ import annotations


class PortSelectionError(RuntimeError):
    """The requested USB identity cannot be resolved unambiguously."""


def _normalized_serial(value: str | None) -> str:
    # Espressif serial numbers may be displayed as colon-separated MACs or as
    # the same hexadecimal digits without separators. Preserve other characters
    # rather than matching arbitrary suffixes or discarding unknown content.
    return (value or "").strip().replace(":", "").replace("-", "").casefold()


def resolve_serial_number(serial_number: str) -> str:
    """Match exactly one native ESP32-S3 USB device; never probe other devices.

    Port enumeration is read-only. The returned path is opened later by the
    vendor SDK, so an unplug between enumeration and open is still an OS error.
    """
    from serial.tools.list_ports import comports

    wanted = _normalized_serial(serial_number)
    if not wanted:
        raise PortSelectionError("--serial-number must contain a USB serial number.")
    try:
        ports = list(comports())
    except Exception as exc:
        raise PortSelectionError(f"Could not enumerate USB serial ports: {exc}") from exc
    candidates = [p for p in ports if (p.vid, p.pid) == (0x303A, 0x1001)]
    matches = sorted({
        p.device for p in candidates
        if _normalized_serial(p.serial_number) == wanted
    })
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise PortSelectionError(
            f"USB serial {serial_number!r} matches multiple ports: {', '.join(matches)}. "
            "Select the intended interface with --port."
        )
    available = "; ".join(
        f"{p.device} (serial {p.serial_number or 'unreported'})"
        for p in sorted(candidates, key=lambda p: p.device)
    ) or "none"
    raise PortSelectionError(
        f"No ESP32-S3 USB serial port matches {serial_number!r}. "
        f"Currently enumerated ESP32-S3 ports: {available}. "
        "If the hand was just reset, let USB enumeration finish and rerun this command."
    )
