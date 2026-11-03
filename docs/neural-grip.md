# How the brain regulates grip

[Getting started](../README.md) · [Configuration](configuration.md) · [Sensors](sensors.md)

Build a grip response from position comparisons, motor primitives, touch, and
inhibition. This guide follows one finger through the included brain so you can
inspect the activity, understand each relationship, and try your own changes.

The reference is the *Introductory Guide to Building an Artificial Brain*,
Expanded developer and agent booklet v0.5, available with the
[Artificial Brains SDK and public guidelines](https://github.com/artificial-brains-inc/artificial_brains).
Chapter and page references below point to that guide. Use the
[SDK README](https://github.com/artificial-brains-inc/artificial_brains/blob/main/README.md)
for application integration and [AGENTS.md](https://github.com/artificial-brains-inc/artificial_brains/blob/main/AGENTS.md)
for construction conventions.

## Open the example

From the repository root, after installation:

```bash
source .venv/bin/activate
python run.py --check
ab design --brain brain.abdesign.json
```

The walkthrough below follows the index finger in that full-hand graph. The
Python blocks in sections 3–5 form one complete design when run in order.
The same code is provided in [examples/index_grip.py](../examples/index_grip.py),
so you can generate and inspect it directly:

```bash
python examples/index_grip.py
ab design --brain examples/index-grip.abdesign.json
```

This creates a separate teaching design without changing `brain.abdesign.json`.
Use the **full-hand** design with `run.py`; the one-finger design has a different
input/output contract. To build and run the full hand, follow
[Explore and build the brain](../README.md#3-explore-and-build-the-brain).

## 1. The computational building blocks

An **Oscillatory Processing Unit (OPU)** is a population of synthetic neurons with a shared rhythm and an operating point, TNorm. Computation belongs to directed source-to-target relationships, not to a rhythm label alone. The booklet calls the resulting neural state an **Oscillatory Occupancy State (OOS)** (chapter 01, p. 1).

TNorm selects a neural operating point. Keep **2.01** distinct from 2: that precision is part of the motor primitive. Inhibitory `strength=1` uses the calculated reference strength; the resulting suppression depends on the connected activity. TNorm, inhibitory strength, and physical motor gain are separate settings (chapters 04 and 06, pp. 3 and 6).

The body supplies observations. The brain compares desired and measured positions and produces motor and inhibition states. The application handles sensor scaling, motor-command translation, and mechanical limits. Following these responsibilities separately makes the activity easier to interpret.

## 2. Circuit inventory

| Component | Count and representation |
| --- | --- |
| Desired actuator positions | 7 input populations, each 64 elements |
| Measured actuator positions | 7 input populations, each 64 elements |
| Fingertip contact | 5 input populations, each 64 elements |
| Motor primitives | 14: one FLEX and one EXTEND per motor |
| Motor outputs | 14 one-neuron bistable outputs |
| Grip-HOLD outputs | 5 one-neuron bistable outputs |
| Internal OPUs | 109 |
| Total named populations | 147 |
| Connections | 236 |

There are **19 external inputs and 19 outputs**. Each input carries a goal or physical observation; comparisons and tactile opposition are internal populations. Outputs stream continuously without an ACK population. All input widths are 64, below the booklet's recommended 156-element maximum for these receiving Theta pathways. The build calculates the total neuron allocation; a population contains multiple neurons (chapter 05, p. 4).

The graph retains the abduction pair for a uniform I/O contract, but the physical adapter ignores its movement and HOLD requests while the manual thumb lock is in force. The other two thumb motors and four tendon motors are active.

## 3. Give the brain an intention and a body observation

The UI supplies seven actuator coordinates, either entered directly or obtained from a named posture such as Grab. Vendor geometry converts a compact joint pose into actuator goals. These coordinates are the intention supplied to the brain; its ongoing neural comparisons generate the movement toward them.

Desired and measured angles are independently mapped into the same actuator-range representation. No desired-minus-measured error is calculated by the encoder. For the index motor, the input names are `goal_index_tendon` and `position_index_tendon`.

Each enters its own **Theta T2 representation**, then two separate **Theta T2 comparison OPUs**. The matching immediate sources are important: the representation and comparison remain distinct populations even though their rhythm and TNorm match (chapter 06, p. 5; chapter 09, p. 10).

```python
from ab import Brain

brain = Brain("Index finger tactile grip")
brain.input("goal_index_tendon", 64)
brain.input("position_index_tendon", 64)
brain.opu("goal_index_tendon_canonical", "theta", 2)
brain.opu("position_index_tendon_canonical", "theta", 2)
brain.opu("index_tendon_compare_flex", "theta", 2)
brain.opu("index_tendon_compare_extend", "theta", 2)

brain.connect("goal_index_tendon", "goal_index_tendon_canonical")
brain.connect("position_index_tendon", "position_index_tendon_canonical")
brain.connect("goal_index_tendon_canonical", "index_tendon_compare_flex",
              role="operand", units=1)
brain.connect("position_index_tendon_canonical", "index_tendon_compare_flex", "inh",
              role="operand", units=1)
brain.connect("position_index_tendon_canonical", "index_tendon_compare_extend",
              role="operand", units=1)
brain.connect("goal_index_tendon_canonical", "index_tendon_compare_extend", "inh",
              role="operand", units=1)
```

The matched contributions oppose each other, leaving activity for the direction of the discrepancy. Follow that activity over the receiving rhythm: a single silent frame can occur during a rhythmic response and is not enough to establish that the positions match (chapter 09, p. 10).

## 4. Turn comparison evidence into motor activity

For each motor and each direction:

```text
Theta T2 comparison
    -> Theta T3 preparation
    -> Theta T2.01
    -> Beta T5 motor
    -> FLEX or EXTEND output
```

The **Theta T3 → Theta T2.01 → Beta T5 → output** relationship follows the motor primitive in chapters 08 and 13 (pp. 7–8 and 15). FLEX and EXTEND each have a complete path; other fingers can act simultaneously.

A Beta T3 reset, driven by that direction's motor Beta T5, inhibits its own Theta T2.01 and its output. FLEX motor Beta T5 also inhibits EXTEND Theta T2.01 and the EXTEND output, and EXTEND does the reverse. Every explicit reset and antagonist inhibition in this graph uses `strength=1.0`.

This makes three states worth distinguishing: the comparison's evidence, the motor OPU's activity, and the output's held state. A bistable output can outlast the activity that activated it. Clearing the internal pathway alone is insufficient (chapter 13, p. 16).


Add both motor pathways to the same `brain` object:

```python
brain.opu("index_tendon_flex_theta3", "theta", 3)
brain.opu("index_tendon_flex_theta2", "theta", 2.01)
brain.opu("index_tendon_flex_beta5", "beta", 5)
brain.opu("index_tendon_flex_reset", "beta", 3)
brain.output("index_tendon_flex")
brain.opu("index_tendon_extend_theta3", "theta", 3)
brain.opu("index_tendon_extend_theta2", "theta", 2.01)
brain.opu("index_tendon_extend_beta5", "beta", 5)
brain.opu("index_tendon_extend_reset", "beta", 3)
brain.output("index_tendon_extend")

brain.connect("index_tendon_compare_flex", "index_tendon_flex_theta3")
brain.connect("index_tendon_flex_theta3", "index_tendon_flex_theta2")
brain.connect("index_tendon_flex_theta2", "index_tendon_flex_beta5")
brain.connect("index_tendon_flex_beta5", "index_tendon_flex")
brain.connect("index_tendon_flex_beta5", "index_tendon_flex_reset")
brain.connect("index_tendon_flex_reset", "index_tendon_flex_theta2", "inh",
              role="feedback", strength=1)
brain.connect("index_tendon_flex_reset", "index_tendon_flex", "inh", strength=1)

brain.connect("index_tendon_compare_extend", "index_tendon_extend_theta3")
brain.connect("index_tendon_extend_theta3", "index_tendon_extend_theta2")
brain.connect("index_tendon_extend_theta2", "index_tendon_extend_beta5")
brain.connect("index_tendon_extend_beta5", "index_tendon_extend")
brain.connect("index_tendon_extend_beta5", "index_tendon_extend_reset")
brain.connect("index_tendon_extend_reset", "index_tendon_extend_theta2", "inh",
              role="feedback", strength=1)
brain.connect("index_tendon_extend_reset", "index_tendon_extend", "inh", strength=1)

brain.connect("index_tendon_flex_beta5", "index_tendon_extend_theta2", "inh",
              role="feedback", strength=1)
brain.connect("index_tendon_flex_beta5", "index_tendon_extend", "inh", strength=1)
brain.connect("index_tendon_extend_beta5", "index_tendon_flex_theta2", "inh",
              role="feedback", strength=1)
brain.connect("index_tendon_extend_beta5", "index_tendon_flex", "inh", strength=1)
```

## 5. Let touch oppose closing

The index's physical pad supplies `touch_index`, not a computed label such as `ENOUGH_GRIP`. Its neural chain is:

```text
touch_index
    -> touch_index_theta3  [Theta T3]
    -> touch_index_theta2  [Theta T2.01]
    -> grip_index_opposition  [Beta T5]
```

The opposition population inhibits **index FLEX Theta T2.01** and the **index FLEX output**, and excites the separate **grip_index_hold** output. It does not inhibit the extension path.

```python
# Continue with the position comparisons and both motor paths above.
brain.input("touch_index", 64)
brain.opu("touch_index_theta3", "theta", 3)
brain.opu("touch_index_theta2", "theta", 2.01)
brain.opu("grip_index_opposition", "beta", 5)
brain.opu("grip_index_reset", "beta", 3)
brain.output("grip_index_hold")
brain.opu("grip_index_hold_relay", "high-gamma", 10)

brain.connect("touch_index", "touch_index_theta3")
brain.connect("touch_index_theta3", "touch_index_theta2")
brain.connect("touch_index_theta2", "grip_index_opposition")
brain.connect("grip_index_opposition", "index_tendon_flex_theta2", "inh", strength=1)
brain.connect("grip_index_opposition", "index_tendon_flex", "inh", strength=1)
brain.connect("grip_index_opposition", "grip_index_hold")

brain.connect("grip_index_opposition", "grip_index_reset")
brain.connect("grip_index_reset", "touch_index_theta2", "inh", role="feedback", strength=1)
brain.connect("grip_index_reset", "grip_index_hold", "inh", strength=1)
brain.connect("grip_index_hold", "grip_index_hold_relay")
brain.connect("grip_index_hold_relay", "index_tendon_flex_theta2", "inh",
              role="feedback", strength=1)
brain.connect("grip_index_hold_relay", "index_tendon_flex", "inh", strength=1)
```

The HOLD relay supplies ongoing closing inhibition while the HOLD output is active. Its high-gamma operating point is explicitly T10. The Beta T3 reset clears both the tactile Theta T2.01 state and the HOLD output. Continued touch can re-excite the path. It is not an irreversible latch.

The ordinary HOLD-output-to-relay excitation is the relay's forward drive. The relay's inhibitory return to the motor OPU is marked feedback; output-clearing edges do not receive OPU-only roles. Keep those roles when reusing the pathway.

Each pad regulates its finger. Thumb touch connects to the thumb's three FLEX paths in the graph; the adapter's manual abduction lock keeps that one physical axis fixed.

Save the one-finger design after adding the tactile pathway:

```python
from pathlib import Path

output = Path("examples/index-grip.abdesign.json")
output.parent.mkdir(parents=True, exist_ok=True)
brain.save(str(output))
```

The complete script performs these declarations in order, validates the public
design, and saves to that location. It does not connect to hardware or start a
neural session.

## 6. What "enough grip" means in this example

The coordinate comparison supports further closing. Tactile activity supplies opposition within the circuit. Their evolving neural states determine the motor and HOLD outputs. As the physical finger moves, both its position and its contact observation change, and the interaction continues.

Here, "enough" means **the tactile pathway has recruited a state that suppresses additional closure and may request grip-HOLD**. You provide a target posture rather than a per-object pressure setting. The response is shaped by the operating points, connections, current sensory activity, and recent neural state.

This example explores self-regulation through that feedback loop. Its contact scale is relative, and the circuit does not estimate force in newtons or object weight. It has no explicit association-memory primitive: adapting activity during contact and learning a persistent association are different experiments (chapter 02, p. 2).

## 7. Why the driver executes a neural HOLD

The hand accepts persistent **position targets**. Suppose the motor is measured at 80 degrees while the most recent command is 88 degrees. Silencing FLEX would prevent new increments, but the motor could still pursue 88 degrees.

A neural grip-HOLD tells the adapter to cancel remaining closing travel. It moves the target back toward the current measured position without moving an already opening target farther into closure, suppresses simultaneous closing increments, and preserves neural extension. HOLD is decoded **before** opposing motor counts are added, so a simultaneous extension is not cancelled by a flex count that HOLD has suppressed.

This rule is body-command translation, implemented in `body_commands.py` and `hand.py`. The brain triggers it through a named output; raw pressure is not passed to `apply_deltas()`. The actuator electronics execute the resulting target. HOLD is not torque disable and does not command an exact force.

The UI's whole-hand Hold, startup opening, manual thumb positioning, serial recovery, joint bounds, and target-lead limiting are separate host operations. The movement log distinguishes these from a neural grip-HOLD, following the controller role described in chapter 13, p. 16.

## 8. Bring touch into the brain

The host acquires an unloaded baseline and idle-noise band, fixes them for the run, then scales each contact signal using remaining ADC headroom. For a positive-polarity sensor the normalized observation is:

```text
clip((raw_adc - unloaded_baseline - noise_band)
     / (4095 - unloaded_baseline - noise_band), 0, 1)
```

Negative polarity reverses the observed change and uses headroom toward zero. The noise band removes idle variation; it is separate from the neural grip response. Compare the raw and normalized readings in the UI when checking a sensor.

The normalized contact amount becomes a 64-element cumulative vector with a fractional final bin. Zero produces all zeros; small observations remain small. `vector_f32` supplies those amplitudes to the SDK. The vector encodes one physical sensor; the receiving populations form its neural response. It is distinct from the internal fuzzy-condition banks described in chapter 09.

## 9. Output timing and motor gains

The example consumes completed output frames, including empty and repeated active frames. Each fresh active motor count contributes its configured degree increment; a held output can therefore advance the target over several frames. A reset/clearing path is essential to the intended action lifetime.

Input polling is paced to observed transport throughput and capped by configuration. Neural rhythms run independently of the polling rate. With continuous outputs and no ACK, an output frame is not explicitly matched to an individual observation. During startup, the application receives 65 completed events with motor writes disabled while pending input observations drain. Use one input-producing application per device.

A slow finger may have sparse neural output, strong opposition, a HOLD latch, stale feedback, or a host movement limit. Increasing `--gain` changes physical displacement per count; it does not change TNorm or create missing neural activity.

## Experiments to try

### Follow one finger

Start in observation mode or with an empty hand. Observe the index goal/position representations, the two comparison populations, motor Theta T2.01, motor Beta T5, FLEX/EXTEND outputs, tactile opposition, and HOLD together. Examine complete activity patterns over time.

Then compare unloaded contact with a modest pad press. Inspect whether the tactile state changes, FLEX is inhibited, HOLD appears and clears, and opening remains available. In a live trial, compare `outputs.jsonl` with `motion.jsonl`: `neural_grip_hold` is different from `target_lead` or `coupled_joint_model`.

### Change tactile opposition

In `aerohand_control/design.py`, `VETO_STRENGTH` sets the tactile-to-FLEX inhibition and is initially `1.0`. Change that relationship while keeping the matched position-comparison operand units at 1. Regenerate and rebuild, then compare the tactile opposition, motor activity, output clearing, and resulting movement. Changing a subtraction operand changes the comparison itself, so study it as a separate experiment.

Use these commands after a neural edit:

```bash
python make_brain.py
python run.py --check
ab design --brain brain.abdesign.json
ab build brain --brain brain.abdesign.json --env .env
```

Wait for READY, then launch the application with the run command in the [README](../README.md#4-run-the-hand).

### Connect a new observation

Add a meaningful sensor signal and let an internal sensory pathway carry it to the motor circuitry. Reuse complete source-to-target relationships, preserve the clearing paths, and let the build check the neuron budget. Observe the new source, its receiving population, the motor output, and the physical response together. The booklet's sensory, decision, and motor chapters provide further starting relationships to explore.
