# Explore one finger

[Neural-grip walkthrough](../docs/neural-grip.md) · [Full-hand setup](../README.md)

`index_grip.py` is the complete public-SDK construction for one index-finger
position/touch circuit. It uses the same populations, TNorms, operand units,
resets, and connections as the index-finger portion of the full hand.

From the repository root:

```bash
source .venv/bin/activate
python examples/index_grip.py
ab design --brain examples/index-grip.abdesign.json
```

The generated diagram has three external inputs (goal, measured position,
touch), two directional motor outputs, and one grip-HOLD output. The script
only generates a design; it does not move hardware. Use the root
`brain.abdesign.json` for the seven-motor `run.py` application.
