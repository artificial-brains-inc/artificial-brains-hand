# Testing the example

The software tests let you work on the circuit, sensor interface, motor adapter,
and UI without connecting a physical hand. Hardware checks then let you inspect
how your assembled system behaves.

## Local software tests

From the repository root, with your virtual environment active:

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
python run.py --check-offline
python tools/check_project.py
```

The test suite supplies synthetic serial packets, sensor samples, SDK boundaries,
and reference geometry. It covers sensor polarity and unloaded zeroing, tactile
encoding, serial recovery, startup ordering, thumb positioning, motor increments,
neural HOLD translation, input pacing, and telemetry recording.

`--check-offline` checks the graph structure and input/output contract.
`check_project.py` checks Python syntax, required project files, local documentation
links, and consistency of the generated neural exports. Neither contacts hardware.

The GitHub Actions workflow runs these software checks on Python 3.10 and 3.12.

## Installed SDK checks

```bash
python -m pip install -r requirements.txt
python run.py --check
```

This uses the installed SDKs to validate the public brain design, check dependency
versions, and check the coordinate conversions and pose definitions. It does not
open the serial ports or start the neural session.

## Browser tests

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python tests/browser_smoke.py
```

The browser script uses the local UI server and synthetic state to exercise pose
buttons, coordinate entry, thumb setup, sensor zeroing, neural-HOLD indicators,
operator Hold/Stop, and desktop/mobile layouts. It writes screenshots to
`test-results/`, which is ignored by Git.

To use an existing Chromium installation:

```bash
CHROMIUM_EXECUTABLE=/usr/bin/chromium python tests/browser_smoke.py
```

## Check your hardware

First follow the README's serial read checks. Then build the current design:

```bash
ab build brain --brain brain.abdesign.json --env .env
```

Wait for READY. Start in observation mode with the two port variables set as
shown in the README:

```bash
AB_TELEMETRY=true python run.py \
  --port "$HAND_PORT" \
  --tactile-port "$TACTILE_PORT" \
  --env .env \
  --observe-only
```

With unloaded pads, capture a zero in the UI. Press one pad at a time and follow
its touch input, opposition population, and grip-HOLD output. Inspect the full
activity pattern over time. Observation mode sends no motor or settings writes.

For a movement trial, stop that session and run without `--observe-only`. Begin
with an empty hand, then a bench object. Compare the measured positions, neural
outputs, contact observations, and applied motor increments in the recorded run.

Software tests cover program behavior; force, contact overshoot, object retention,
and response time are properties to measure on your hardware. Include those
measurements when reporting a physical result.

## Check the local guides

The rendered guides are included, so reading them needs only a browser:

```bash
xdg-open docs/site/index.html
```

After editing a Markdown guide, regenerate the local pages:

```bash
python -m pip install -r requirements-docs.txt
python tools/render_docs.py
python tools/check_project.py
python -m pytest -q tests/test_learning_guide.py
```

The guide tests check all local browser links and heading anchors. They also
execute the walkthrough's Python declarations with a declaration recorder and
compare the resulting graph with the index-finger section of the full design.
When the runtime SDKs are installed, a separate process validates both designs
with those SDKs; otherwise that specific check is reported as skipped.
