# Configuration reference

Run from the repository root. The application defaults to `config.local.json`
when it exists, otherwise `config.json`. Use `--config PATH` to choose explicitly.
`config.local.json`, `.env`, `thumb_setup.json`, `tactile_baseline.json` and logs
are local operating data and are ignored by Git.

## Three different coordinate representations

`poses.json` stores **compact joint degrees** in this order:

```text
thumb_cmc_abd, thumb_cmc_flex, thumb_mcp_ip, index, middle, ring, pinky
```

The UI's coordinate fields and the brain's desired/measured inputs use
**logical actuator degrees** in this order:

```text
thumb_cmc_abd, thumb_cmc_flex, thumb_tendon,
index_tendon, middle_tendon, ring_tendon, pinky_tendon
```

These are not interchangeable representations. Vendor forward geometry maps the
compact joint posture to motor targets, accounting for thumb coupling. Motor
orientation is applied consistently before coupled conversion; feedback and
serial writes use the corresponding inverse/reflection. Native **hardware
actuator degrees** are the values actually sent to the serial interface.
Measured actuator positions are not direct measurements of all finger joints.

## Host configuration

| Field | Default | Interpretation |
| --- | --- | --- |
| `motor_directions` | `[-1,1,1,1,1,1,1]` | Coordinate orientation, seven entries in motor order. `-1` reflects that axis; verify against your setup. |
| `per_spike_degrees` | `[0.35,0.5,0.75,0.8,0.8,0.8,0.8]` | Actuator-target increment per decoded output count, before global gain and limits. Not neural connection weights. |
| `input_hz` | `20` | Upper acquisition setting. Actual capture is conservatively paced by recent output receipt throughput, at most 20 Hz. Not the neural iteration frequency. |
| `telemetry_timeout_s` | `0.15` | Runtime hand-position read deadline. A missed reply can recover later. |
| `startup_timeout_s` | `5` | First hand-position read deadline. |
| `output_timeout_s` | `2` | Watchdog for missing neural output frames. Empty frames are valid frames. |
| `max_output_age_s` | `0.5` | Maximum age of queued decoded output before the controller refuses to replay it. |
| `command_timeout_s` | `null` | No automatic command duration by default. A positive value requests an operational Hold after that duration. |
| `status_interval_s` | `1` | Terminal status interval. |
| `max_target_lead_degrees` | `8` | Limits pending motor target lead relative to actual position, per motor. It is not a force threshold or neural decision. |
| `tactile_max_age_s` | `0.25` | Maximum age of a current tactile sample. Missing data are not replaced by zero. |
| `tactile_directions` | `[1,1,1,1,1]` | Sensor polarity in thumb-to-little order. `1` means ADC increases under contact; `-1` means it decreases. |
| `feedback_reuse_s` | `0.04` | Maximum time a measured hand-position packet may be reused for nearby output events. |
| `logging_max_mb` | `256` | Per-run log cap in MiB; status reports drops/full disk errors. |
| `startup_open_timeout_s` | `30` | Deadline for the other six motors to reach the configured Open pose. |
| `startup_open_tolerance_degrees` | `2` | Per-motor arrival tolerance for startup; manual thumb abduction is excluded. |

The target-lead handling is nonfatal for **all motors**. When an existing target
is already outside the window, the adapter retains it, records the discrepancy,
and does not advance it farther outward. Neural requests back toward the measured
position and explicit grip-HOLD remain possible. Other motors continue. It does
not manufacture a corrective motion from zero neural output. This is a pending
travel limit, not a guarantee of safe force.

## Environment and command-line settings

`.env` supplies `AB_DEVICE_URL`, `AB_DEVICE_API_KEY`, `AB_DEVICE_TIMEOUT`, and
optional `AB_TELEMETRY`. Use the address and developer key from your registered device.

`--port` or `--serial-number` selects the hand. `--tactile-port` selects the
separate sensor board (`auto` only when uniquely identifiable). `--env` selects
the credentials file. `--baseline`, `--thumb-setup`, and `--log-dir` select paths
for locally saved state. `--brain` selects the graph whose contract must match
the application and whose hash must match the installed device graph.

`--gain` multiplies the per-count movements (default 4). `--speed` (0..32766)
and `--torque` (0..1000) explicitly invoke vendor settings for all motors; omission
leaves those settings unchanged. Do not interpret the vendor torque number as a
calibrated force in newtons. The application does not enable maximum torque by
default. `--duration` requests a timed Hold for a trial.

## Manual thumb setup

First live startup preserves the measured abduction position unless a saved
`thumb_setup.json` exists. That file stores logical degrees and the orientation
in use. Changing orientation invalidates a saved thumb setting; re-establish it
under the new convention rather than silently reflecting saved data.

While in Hold: adjust the slider or number, then Apply & save. Slider editing
alone sends no command. The +/-1 buttons command and save small changes. Lock
measured position saves what the actuator actually reached. All normal poses,
coordinate targets and neural outputs retain this locked axis. Thumb flexion
and tendon activity remain neural. Initial opening is still a direct host
setup command for the other six motors, not a neural decision.

## Unloaded zero, not an object-specific grip target

Normal live startup captures unloaded offsets/noise after opening and saves
`tactile_baseline.json`. Keep the hand empty. The same baseline stays fixed
throughout the grasp. To redo it, select Hold and click Zero unloaded sensors.

A first checkout contains no baseline file. Observation mode can acquire readings
without it but labels the scale uncalibrated; capture unloaded zero in the UI
before interpreting contact. `--no-startup-zero` deliberately reuses a saved file
and requires one to exist. Do not copy another rig's baseline.

If pressing a pad lowers its reading, change that channel's `tactile_directions`
entry and capture a new unloaded zero. Check physical channel order by pressing
one pad at a time. Sensor attachment pressure can change the unloaded offset.

## Editing the brain versus the body

Edit `poses.json` to author a posture. Edit `config.local.json` for body and
transport parameters. Edit `aerohand_control/design.py` for neural relationships,
TNorms, operand units and inhibition. Keep neural behavior in the graph and body/transport settings in the adapter so
you can inspect their effects separately.

After a neural change:

```bash
python make_brain.py
python run.py --check
ab design --brain brain.abdesign.json
ab build brain --brain brain.abdesign.json --env .env
```

Wait for READY and run a fresh application. Build again when switching projects
on a shared device, even when a local file has not changed. Stop/save normally;
reset dynamics only when deliberately discarding the brain's accumulated state.
