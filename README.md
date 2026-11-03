# Artificial Brains Tactile Hand

**Explore biomimetic grip control with synthetic neurons.**

Give a robotic hand a target posture and let its artificial brain respond to touch. Position comparisons support movement toward the target, while fingertip feedback opposes further closing. The behavior comes from connected neural primitives interacting with a physical body, without a training dataset or pre-training step.

**GET YOUR SYNTHETIC NEURONS:** [Prima-Lux Dev Kit with up to 10,000 synthetic neurons](https://artificialbrains.ai)

Clone the example, run it on your Prima-Lux, and use it as a starting point for your own projects. You can explore the brain on the Canvas, observe individual neurons, change the circuit, and share what you build.

The project includes the brain design, a browser control panel, five-channel sensor firmware, telemetry recording, and a [hands-on guide to the neural circuit](docs/neural-grip.md).

[View the control panel](docs/assets/ui-preview.png) · [Learn how the grip works](docs/neural-grip.md)

## What you need

| Component | Setup |
| --- | --- |
| **Prima-Lux dev kit** | A registered 10,000-neuron device, its developer API key, and device software supporting the SDK 0.4.0 live output stream. |
| **Aero Hand Open** | A powered, configured hand with vendor firmware and homing completed. Follow the [hand setup guide](https://docs.tetheria.ai/docs/sdk/). |
| **Five touch sensors** | One pad per finger, connected to a USB acquisition board. The included firmware supports a Blue Pill STM32F103C8 using PA0–PA4. See [sensor setup](docs/sensors.md) for wiring requirements and the data format. |
| **Computer** | Linux/Ubuntu, Python 3.10 or newer, Git, Python venv support, and a browser. |
| **Connections** | Network access to Prima-Lux and separate data USB connections for the hand and sensor board. Internet access is needed to install dependencies and use the device build workflow. |

The acquisition firmware works with analog contact readings. Choose sensors and conditioning circuitry suitable for your board's electrical limits; the project leaves the sensor model and circuit to your hardware setup. A board that already produces the [supported CSV format](docs/sensors.md#data-format) can be used directly.

## 1. Clone and install

On Ubuntu, install the system prerequisites once:

```bash
sudo apt update
sudo apt install git python3 python3-venv
```

Clone from GitHub:

```bash
git clone https://github.com/artificial-brains-inc/artificial-brains-hand.git
cd artificial-brains-hand
```

### From a downloaded Git bundle

A Git bundle contains the same repository. To start from the downloaded bundle
instead of GitHub, use this clone command in a directory for your projects:

```bash
git clone "$HOME/Downloads/artificial-brains-hand-v0.0.0.bundle" artificial-brains-hand
cd artificial-brains-hand
```

Use either clone method, then install into a new environment in that checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m ensurepip --upgrade
python -m pip install -r requirements.txt
python -m pip check

cp .env.example .env
python run.py --check
```

`requirements.txt` installs the Artificial Brains SDK, Aero Hand SDK, and serial
library. `--check` validates the design and installed SDK interfaces without
connecting to hardware. All paths in this guide are relative to this checkout;
no previous project or environment is needed.

For a look at the controls before connecting your equipment:

```bash
python run.py --preview-ui
```

You can read the [neural-grip guide](docs/neural-grip.md) directly on GitHub.
A browser-readable copy is included for local use, with the same guide and
working navigation:

```bash
xdg-open docs/site/index.html
```

## 2. Configure your device

Open `.env` and enter the address and developer API key from your Prima-Lux Device Manager:

```dotenv
AB_DEVICE_URL=http://prima-lux.local:8080
AB_DEVICE_API_KEY=YOUR_DEVICE_API_KEY
AB_DEVICE_TIMEOUT=2
AB_TELEMETRY=true
```

An IP address can replace `prima-lux.local`. Keep your API key in this local file; `.env` is ignored by Git. The [SDK repository](https://github.com/artificial-brains-inc/artificial_brains) covers device registration and includes the public developer guidelines.

### Connect the hand and sensors

List the available serial devices:

```bash
python -m serial.tools.list_ports -v
ls -l /dev/serial/by-id/
```

Identify the **hand** and the **separate sensor board**, then set their paths in your terminal. These are example port assignments; use the paths reported on your computer:

```bash
export HAND_PORT="/dev/ttyACM1"
export TACTILE_PORT="/dev/ttyACM0"
```

A `/dev/serial/by-id/...` path is useful for keeping the same device selection after reconnecting USB. Close other applications using either port, then check the readings:

```bash
python run.py --read-positions --port "$HAND_PORT"
python run.py --read-tactile --tactile-port "$TACTILE_PORT"
```

The first command prints seven actuator positions; the second prints five raw touch readings. Neither moves the hand or starts the brain. The finger order is **thumb, index, middle, ring, little**.

For a uniquely identified STM32/Maple sensor board, you can also use `--tactile-port auto`. The hand can be selected with `--serial-number YOUR_HAND_USB_SERIAL` instead of `--port`. See [configuration](docs/configuration.md) for these options and [troubleshooting](docs/troubleshooting.md) for serial permissions.

### Match the configuration to your hand

The supplied settings are in `config.json`. To keep your own settings separate:

```bash
cp config.json config.local.json
```

The application automatically uses `config.local.json` when it exists. Check these two mappings:

- **`motor_directions`** follows the seven-motor order documented in [configuration](docs/configuration.md). The supplied `[-1, 1, 1, 1, 1, 1, 1]` uses a reversed logical convention for thumb abduction.
- **`tactile_directions`** follows thumb-to-little order. Use `1` when a pad's reading increases under contact, or `-1` when it decreases.

Thumb abduction has its own setup control in the UI and stays fixed during the grasp. The other six motors receive neural movement commands.

## 3. Explore and build the brain

Open the supplied design on the Canvas:

```bash
ab design --brain brain.abdesign.json
```

Follow one finger's desired-position, measured-position, motor, and touch pathways. Then build the design on Prima-Lux:

```bash
ab build brain --brain brain.abdesign.json --env .env
```

Wait for **READY** before running. Build again whenever you change the neural design or switch brains on the device. Run one controller per Prima-Lux at a time.

## 4. Run the hand

Keep the hand empty and the sensor pads clear during startup. The application opens the six active motors, captures the unloaded sensor readings, and then starts the brain. Begin with bench objects and keep your hands clear of moving parts; [bench setup](docs/safety.md) covers operating precautions.

```bash
AB_TELEMETRY=true python run.py \
  --port "$HAND_PORT" \
  --tactile-port "$TACTILE_PORT" \
  --env .env \
  --gain 4
```

The browser control panel opens automatically. `run.py` starts the neural session, so there is no separate `ab run` step.

On first launch, thumb abduction stays at its measured position. While the panel shows **Hold**, use **Thumb position** to place it for your grasp, then select **Apply & save** or **Lock measured position**. That setting is reused on subsequent runs.

| Control | Use it to |
| --- | --- |
| **Pose buttons** | Select Open, Grab, Pinch, or another included gesture. |
| **Posture amount** | Set how far to move toward the next selected pose. This changes posture, not grip pressure. |
| **Actuator coordinates** | Enter a target for each motor; the locked thumb-abduction position is preserved. |
| **Zero unloaded sensors** | Refresh the unloaded baseline while the hand is empty and in Hold. Startup does this automatically. |
| **Hold / Space** | Hold the measured hand position while keeping the neural session available. |
| **Stop brain** | Hold the hand, stop the neural session, and save its dynamics. |

The default gain is `4`. Gain scales the actuator increment produced by each neural output count; use a different `--gain` value when your hardware or experiment calls for it. The [configuration guide](docs/configuration.md) explains gains, saved thumb setup, and sensor polarity.

To finish, select **Stop brain** or press Ctrl+C in the terminal. Wait for the Stop/save confirmation before disconnecting Prima-Lux. Closing the browser requests Hold but leaves the neural session running.

### Observe without applying motor commands

Use the same inputs and real neural output stream with motor writes disabled:

```bash
AB_TELEMETRY=true python run.py \
  --port "$HAND_PORT" \
  --tactile-port "$TACTILE_PORT" \
  --env .env \
  --observe-only
```

This mode skips physical opening and automatic zeroing. On a fresh setup, leave the pads unloaded and select **Zero unloaded sensors** in the UI. You can then press each pad and follow its neural response. `--cli` selects terminal controls; `--no-browser` prints the UI address without opening it.


## How the brain adjusts the grip

Each motor has two complete directional primitives: one for **FLEX** and one for **EXTEND**. Desired and measured positions enter separately and feed opposing **Theta T2** comparisons. Each comparison supports a motor chain:

```text
Position comparison → Theta T3 → Theta T2.01 → Beta T5 → motor output
```

Each touch sensor has a sensory pathway:

```text
Touch → Theta T3 → Theta T2.01 → Beta T5 opposition
```

The position comparison supports reaching the target. The tactile opposition inhibits that finger's closing pathway and its FLEX output. It also produces a **grip-HOLD** output, which tells the hand driver to cancel any closing travel still pending. Opening remains available.

Local **Beta T3 reset** pathways clear the active motor and tactile states, including their bistable outputs. Continuing observations can recruit another response. The hand changes position and contact as it moves, and those new observations return to the brain. This ongoing interaction is the grip-regulation loop.

The controller supplies observations, zeroes unloaded sensor offsets, and translates neural outputs into motor commands. **The brain produces the position comparisons, tactile inhibition, and grip-HOLD signals.** Mechanical travel limits and the manual thumb lock remain in the driver.

There is no per-object pressure target to capture. Here, “enough grip” is the tactile response opposing further closure, rather than a force value prescribed by the application. Readings are relative contact measurements, not newtons.

Follow the [neural-grip walkthrough](docs/neural-grip.md) to build these relationships, inspect their activity, and experiment with the operating points described in the public guidelines.

## Make it your own

| Change | Edit |
| --- | --- |
| A gesture or target posture | `poses.json` |
| Motor gains, sensor polarity, or transport settings | `config.local.json` |
| Neural populations, TNorms, connections, or inhibition | `aerohand_control/design.py` |
| Input encoding and output decoding | `aerohand_control/neural.py` |
| The control panel | `aerohand_control/web/` |

After editing the neural generator, regenerate the exports, inspect the circuit, and rebuild:

```bash
python make_brain.py
python run.py --check
ab design --brain brain.abdesign.json
ab build brain --brain brain.abdesign.json --env .env
```

Wait for READY, then run the hand again. `brain_source.py` is the generated literal Python design; `design.py` is the editable generator. Follow the [neural-grip experiments](docs/neural-grip.md#experiments-to-try) to change one relationship at a time and observe the result.

## Telemetry and logs

With `AB_TELEMETRY=true`, the SDK opens the neuron viewer alongside the controls. Inspect a population's firing history while selecting a pose or pressing a pad. **Freeze pauses the viewer, not the hand or brain.**

Every hardware run records data under `logs/run-<timestamp>-<id>/`:

| File | Recorded data |
| --- | --- |
| `sensors.jsonl` | Touch readings, positions, targets, and published observations. |
| `outputs.jsonl` | Neural output frames. |
| `motion.jsonl` | Requested and applied movement, with per-motor limit reasons. |
| `neurons.jsonl` | Neuron layout and activity from the device telemetry stream. |
| `events.jsonl` | Commands, startup, zeroing, recoveries, and shutdown. |
| `run.json` | Run settings, graph identity, and recording status. |

The default cap is 256 MiB per run. The UI reports stream availability and recording status. Use `--no-neuron-log` to keep the other logs without the full-neuron recording.

After stopping, summarize or package a run:

```bash
python tools/summarize_logs.py
python tools/pack_logs.py
```

Review logs for device identifiers and local paths before sharing. For a quiet finger, compare neural outputs with applied movement: `neural_grip_hold` identifies the tactile command, while `target_lead` identifies an adapter restriction. See [troubleshooting](docs/troubleshooting.md).

## Learn, test, and contribute

Explore the [public guidelines and SDK](https://github.com/artificial-brains-inc/artificial_brains), the [neural-grip guide](docs/neural-grip.md), [configuration reference](docs/configuration.md), and [sensor setup](docs/sensors.md).

To run the software checks:

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
python tools/check_project.py
```

[Testing](docs/testing.md) covers the checks and optional browser tests. Improvements, examples, and observations are welcome; see [Contributing](CONTRIBUTING.md).

## License

You are free to clone, use, modify, and redistribute this example for projects using **Artificial Brains products**, including commercial projects, under the [Artificial Brains Example Code License](LICENSE).

The SDKs, hardware, and vendor firmware retain their own licenses; this license covers the example, not those products. See [third-party notices](THIRD_PARTY_NOTICES.md) and [brand assets](BRANDING.md) for attribution and included materials.
