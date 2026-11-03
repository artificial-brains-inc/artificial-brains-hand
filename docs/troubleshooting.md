# Troubleshooting

Start with the read-only checks in the README. Change one setting at a time so
you can connect each adjustment to the observed response.

## Serial device cannot be opened

List devices with `python -m serial.tools.list_ports -v` and use the hand's USB
serial identity or a stable by-id path. Select a different port for the sensor
board and close any vendor GUI or serial monitor using those ports.

For a permission error on Ubuntu, check the port's group. When it is `dialout`,
add your user, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

Run the application as your normal user.

## No complete GET_POS reply

The read-only command isolates the hand connection from Prima-Lux. The adapter
parses fragmented position packets and accompanying firmware text. Runtime
position-read timeouts can recover; missed observations do not become new
measurements, and skipped motor events are not replayed later.

If the device prints a ROM downloader message, the application firmware is not
responding. Follow the hand/ESP32 vendor's recovery instructions, then repeat
the position read. Firmware recovery is separate from running this application.

## Opening does not complete

Live startup opens the six controlled motors before neural control. Thumb
abduction is held at the measured/saved setup angle and excluded from the opening
arrival check. Inspect the reported goal, measured angle and error for the other
motors. Check homing, orientation and unloaded mechanics against vendor tools.
Hold and Stop remain usable during setup. The neural session starts after the
opening check succeeds.

## Thumb abduction interferes with the grasp

Select Hold and use the dedicated Thumb position panel. Save the reached measured
position rather than repeatedly pursuing an unreachable target. Ordinary pose
commands and neural outputs cannot move that manually locked axis. Its target
and measurement may differ; both are displayed. Inspect coupling and motor
orientation rather than interpreting logical actuator coordinates as finger joints.

## A fingertip reads contact while unloaded

Remove contact, leave the hand empty, select Hold and click Zero unloaded sensors.
Check channel order and polarity by pressing one pad at a time. The captured
baseline and noise band are fixed during a grasp. A high raw offset can be
normal for a particular circuit, but saturation or insufficient remaining ADC
headroom requires inspecting the actual electronics.

## A finger is quiet or slow

Compare these separate facts in the UI and logs:

- No FLEX/EXTEND output: inspect goal/position encoding, comparison OPUs, local
  motor reset and antagonist paths. A larger driver gain cannot create activity.
- Neural HOLD present: follow the tactile input, opposition OPU, HOLD latch and
  its reset. Compare the raw readings, unloaded baseline, and pad contact.
- Neural movement requested but not applied: inspect `limit_reasons` in
  `motion.jsonl`. `target_lead`, `target_lead_existing`, `coupled_joint_model`,
  `actuator_range` and `manual_thumb_lock` are host restrictions.

The target-lead condition is nonfatal for every motor. It blocks further outward
advance on the affected axis, preserves appropriate neural return/extension and
HOLD, and lets other axes continue. It is not a neural grip decision and does not
force the servo to overcome contact. A zero motor output keeps the last target;
a neural HOLD explicitly cancels remaining closing travel.

## Build or session mismatch

Confirm the registered device and `.env`, then build from this project's root:

```bash
python run.py --check
ab build brain --brain brain.abdesign.json --env .env
```

Wait for READY. The application checks that the installed design identity and
I/O contract match its local graph. Reinstall `requirements.txt` if the installed
SDK versions do not match. Contact Artificial Brains support if the required SDK
package is unavailable.

## Input queue overflow or missing output stream

Run only one input-producing application per device. The example paces
acquisitions, publishes fresh frames and accommodates pending observations at
startup. A queue overflow is not retried blindly and does not reset dynamics;
it is reported and the session is stopped/saved where possible. Use device
software compatible with the pinned SDK and keep additional input-producing
applications disconnected during a trial.

Device software must support the public `/api/brain/stream` contract used by the
pinned SDK. Output gaps, changed runs and failed Stop confirmation are reported.
Resolve the connection or device-software mismatch before starting a new trial.

## Logs or neuron viewer are unavailable

The SDK viewer (`AB_TELEMETRY=true`) and disk recording are separate. Full-neuron
recording requires compatible telemetry and records explicit unavailable/gap
states when not supported. The other streams can continue. Check disk permissions,
space, the configured per-run cap, dropped-record counters and recording status.
The viewer's Freeze control freezes the display, not the hand or brain.

After Stop/save:

```bash
python tools/summarize_logs.py
python tools/pack_logs.py
```

Inspect the archive before sharing. Describe the requested pose, sensor placement,
observed movement, config changes, installed SDK versions and whether the run
was observation-only. Keep credentials and local device identifiers out of shared
excerpts.
