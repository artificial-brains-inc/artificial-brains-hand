# Working on the tactile hand example

Read [README.md](README.md) and [the neural-grip guide](docs/neural-grip.md) before
changing the application. Use the [Artificial Brains SDK and guidelines](https://github.com/artificial-brains-inc/artificial_brains)
for neural design conventions.

## Keep the responsibilities clear

The brain performs signed position comparisons, tactile closing opposition,
output clearing, and grip-HOLD generation. Python supplies separately encoded
goals, measured positions, and touch observations, then translates neural outputs
into physical commands.

Sensor zeroing, manual thumb setup, startup opening, operator Hold/Stop, serial
recovery, transport pacing, and mechanical limits belong to the application.
Keep grip decisions in the neural circuit rather than adding Python pressure
cutoffs, error-sign decisions, or candidate-score selection.

## Preserve the primitive relationships

- Retain the complete Theta T3 → Theta T2.01 → Beta T5 motor chains.
- Clear bistable outputs as well as internal motor states in reset and antagonist
  paths. Tactile opposition leaves EXTEND available.
- Give arithmetic operands matched immediate representations, explicit
  `role="operand"`, positive `units`, and the appropriate connection sign.
- Keep inhibition strength, operand units, TNorm, and physical motor gain distinct.
- High-gamma can be used throughout the circuit with an explicit TNorm.

Edit `aerohand_control/design.py`, then regenerate and inspect both exports:

```bash
python make_brain.py
python run.py --check
ab design --brain brain.abdesign.json
ab build brain --brain brain.abdesign.json --env .env
```

Keep population names and widths aligned with the encoder and decoder. Let the
build calculate the neuron allocation. The graph includes an abduction pair for
a uniform motor interface; the adapter preserves its manually selected position.

## Runtime behavior

Keep target-lead handling local to each motor and nonfatal. Preserve the thumb
lock, neural extension, and grip-HOLD. Zero motor output must not create a new
movement request. Read gaps remain missing observations, and stale actuation is
not replayed after recovery. Stop saves dynamics; reset is a separate operation.
Keep callbacks short and logging asynchronous.

## Check changes

```bash
python -m pytest -q
python run.py --check-offline
python tools/check_project.py
```

Run `python run.py --check` with the installed runtime dependencies. For UI work,
run the browser checks in [Testing](docs/testing.md). Keep test fixtures distinct
from physical measurements, and keep credentials and local run data in ignored
files. Use the public SDK interfaces for application integration.

## Contributions and licensing

Follow [Contributing](CONTRIBUTING.md). Keep the example's [license](LICENSE) and
[third-party notices](THIRD_PARTY_NOTICES.md) with redistributed code; SDKs and
hardware remain separately licensed.
