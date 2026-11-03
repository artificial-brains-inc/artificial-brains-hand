# Vendor class state/interface portions: Copyright (c) 2025 Chestnut Robotics, Inc.
# Identified upstream portions remain Apache-2.0; see THIRD_PARTY_NOTICES.md.
"""Open the native ESP32-S3 USB connection without requesting a reset.

This narrowly replaces the constructor of aero-open-sdk==0.1.0.dev1. Vendor
commands and kinematic conversions remain inherited. The opening order follows
Espressif IDF Monitor's ``open_serial(reset=False)`` path:
https://github.com/espressif/esp-idf-monitor/blob/master/esp_idf_monitor/base/serial_reader.py

ESP32-S3 TRM table 33.3-2 defines RTS=1/DTR=0 as reset. Keep both asserted while
opening, then release RTS before DTR. The final 0/0 state clears the download
flag. This does not restart firmware already in the ROM downloader. OS drivers
can still change line states during open; software cannot guarantee no glitch.
"""

from __future__ import annotations

from serial import Serial

from aero_open_sdk.actuations_to_joints import ActuationsToJointsModelCompact
from aero_open_sdk.aero_hand import AeroHand
from aero_open_sdk.aero_hand_constants import AeroHandConstants
from aero_open_sdk.joints_to_actuations import JointsToActuationsModel


def open_native_serial(port: str, baudrate: int, timeout_s: float) -> Serial:
    """Open once, release the control lines in order, and retain incoming data.

    There is no post-open input/output purge. pyserial itself may discard input
    during its OS-level open; bytes arriving after that must reach the parser.
    The caller must supply the selected port rather than probing serial devices.
    """
    if not isinstance(port, str) or not port:
        raise ValueError("A selected AeroHand serial port is required.")
    connection = Serial(
        port=None,
        baudrate=baudrate,
        timeout=timeout_s,
        write_timeout=timeout_s,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    try:
        connection.port = port
        connection.rts = True
        connection.dtr = True
        connection.open()
        # Do not set DTR False first: RTS=1/DTR=0 requests a hardware reset.
        connection.rts = False
        connection.dtr = False
    except BaseException:
        # A partial open or failed line assignment may still own an OS handle.
        # Cleanup must not replace the original initialization error.
        try:
            connection.close()
        except BaseException:
            pass
        raise
    return connection


class ControlledAeroHand(AeroHand):
    """Pinned vendor state and methods with an explicit serial opening path."""

    def __init__(self, port: str, baudrate: int = 921600, timeout_s: float = 0.15):
        # These are the complete non-transport fields of the pinned constructor.
        # Prepare them before opening so a model error cannot strand a live port.
        constants = AeroHandConstants()
        self.joint_names = constants.joint_names
        self.joint_lower_limits = constants.joint_lower_limits
        self.joint_upper_limits = constants.joint_upper_limits
        self.actuation_names = constants.actuation_names
        self.actuation_lower_limits = constants.actuation_lower_limits
        self.actuation_upper_limits = constants.actuation_upper_limits
        self.joints_to_actuations_model = JointsToActuationsModel()
        self.actuations_to_joints_model = ActuationsToJointsModelCompact()
        self.ser = open_native_serial(port, baudrate, timeout_s)
